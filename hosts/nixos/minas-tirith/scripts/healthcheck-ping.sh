# Line 1 = the main aggregate check. Line 2 = OPTIONAL dedicated check for
# CRITICAL hardware events, and it exists because one check cannot do this
# job alone: Healthchecks alerts on state TRANSITION, so once the aggregate
# check is Down for any reason — a failed backup, SMART blindness, too few
# containers — a brand new machine check merely sends another /fail into an
# already-red check and may notify nobody at all. A dedicated check that is
# normally green makes a new critical event an actual up->down transition.
#
# To enable: create a second check on healthchecks.io and append its URL as
# a SECOND LINE of the sops secret. Absent, criticals fall back to the main
# URL and behave as before — degraded, but not broken.
URL="$(head -1 @healthchecksUrlFile@)"
CRIT="$(sed -n 2p @healthchecksUrlFile@)"
[ -z "$CRIT" ] && CRIT="$URL"
STATE="${STATE_DIRECTORY:-/var/lib/healthcheck-ping}"
problems=""
critical=""

# 1. Is the root filesystem still writable? This is exactly what failed
#    on 2026-07-29 while everything else looked healthy.
if ! findmnt -no OPTIONS / | grep -qw rw; then
  problems="${problems}root filesystem is READ-ONLY; "
fi

# 2. Both ZFS pools healthy? (98 TB lives here)
#
#    `zpool status -x` only examines IMPORTED pools, so if storage2 failed
#    to import it is not unhealthy — it is absent, and -x cheerfully says
#    "all pools are healthy" about the one that remains. Assert both by
#    NAME first, then check health.
for p in storage storage2; do
  if ! zpool list -H -o name "$p" >/dev/null 2>&1; then
    problems="${problems}pool $p is NOT IMPORTED; "
  fi
done
pools=$(zpool status -x 2>/dev/null || echo "zpool status failed")
if ! echo "$pools" | grep -q "all pools are healthy"; then
  problems="${problems}zpool: $(echo "$pools" | head -1); "
fi

# 3. Machine checks. Two uncorrectable MCEs (Bank 5 EX unit, Bank 2 L2)
#    were recorded in July 2026 before the memory was brought back to
#    AMD's 2667 spec for 4x dual-rank. If they return, this must shout.
#
#    Report the DELTA, not the total. rasdaemon's database is persistent,
#    so an absolute count latches UNHEALTHY forever the moment a single
#    error is ever recorded — including the July ones we already
#    diagnosed. A permanently-red alert is one you stop reading, which
#    would make this whole file worthless at the exact moment it matters.
#    What we care about is NEW errors since the last check.
#    ⚠️  THE PATTERN HERE IS LOAD-BEARING AND WAS PREVIOUSLY WRONG.
#    This check matched `^[0-9]+ .*(mce|memory)`, requiring the literal word
#    "mce" or "memory" on the line. rasdaemon emits records as
#        <id> <ISO timestamp> error: <description>
#    and THIS BOX'S ACTUAL FAILURES were:
#        1 2026-07-28 ... error: Execution Unit Error, bank=5, ...
#        2 2026-07-28 ... error: L2 Cache Error, bank=2, ...
#    Neither contains "mce" or "memory", so the old pattern counted the two
#    uncorrectable machine checks that nearly killed this machine as ZERO.
#    The check written specifically to catch a recurrence would have stayed
#    green through the recurrence. Verified against a source-faithful
#    fixture: old pattern -> 0, this pattern -> 2.
#    Match RECORD ROWS (id + ISO date), not error vocabulary.
#
#    Latching, not delta-alerting: once new errors are seen, a marker is
#    written and this stays UNHEALTHY until a HUMAN removes it. An
#    uncorrectable MCE on this hardware warrants acknowledgement, and
#    latching removes every path where a dropped ping, an
#    already-failing check, or a Healthchecks state-transition quirk could
#    let one be silently forgotten.
if ! systemctl is-active --quiet rasdaemon; then
  problems="${problems}rasdaemon NOT running — MCE detection is BLIND; "
fi
#    ⚠️  AND THE ROW FILTER MATTERS TOO. Matching every `<id> <ISO-date>`
#    row was the previous attempt, and it over-corrected: ras-mc-ctl emits
#    memory-controller CEs, PCIe AER, CXL AER, extlog, disk and
#    memory-failure rows with that SAME prefix. A single corrected DIMM CE
#    — routine on non-ECC consumer memory — would latch permanently and,
#    because the latch holds the check red, could MASK a later
#    uncorrectable MCE. That trades a false negative for an un-clearable
#    false positive, which is not an improvement.
#
#    So count the `mce_record` table directly out of rasdaemon's own
#    database, which contains exactly machine checks and nothing else.
#    Fall back to parsing only the `MCE events:` SECTION of --errors if the
#    DB is not readable.
mce_db=/var/lib/rasdaemon/ras-mc_event.db
mce=""
mce_out=""
if [ -r "$mce_db" ] \
   && mce=$(sqlite3 "$mce_db" \
        "SELECT COUNT(*) FROM mce_record WHERE (status & 0x2000000000000000) != 0 OR (status & 0x100000000000) != 0;" 2>/dev/null) \
   && [ -n "$mce" ]; then
  mce_out=$(sqlite3 "$mce_db" \
    "SELECT id,timestamp,error_msg FROM mce_record WHERE (status & 0x2000000000000000) != 0 OR (status & 0x100000000000) != 0 ORDER BY id DESC LIMIT 20;" 2>/dev/null || true)
elif raw=$(ras-mc-ctl --errors 2>/dev/null); then
  # Section-scoped fallback: only rows inside the `MCE events` section.
  mce=$(printf '%s\n' "$raw" | awk '
          /^MCE events/                { inmce = 1; next }
          /^[A-Za-z].*events/          { if (!/^MCE events/) inmce = 0 }
          inmce && /^[0-9]+ [0-9]{4}-[0-9]{2}-[0-9]{2}/ { n++ }
          END { print n + 0 }')
  mce_out=$(printf '%s\n' "$raw" | awk '
          /^MCE events/                { inmce = 1; next }
          /^[A-Za-z].*events/          { if (!/^MCE events/) inmce = 0 }
          inmce && /^[0-9]+ [0-9]{4}-[0-9]{2}-[0-9]{2}/ { print }')
fi

case "$mce" in *[!0-9]*|"") mce="" ;; esac
if [ -n "$mce" ]; then
  prev=0
  if [ -r "$STATE/mce.count" ]; then
    prev=$(cat "$STATE/mce.count")
    case "$prev" in *[!0-9]*|"") prev=0 ;; esac
  fi
  if [ "$mce" -gt "$prev" ]; then
    {
      echo "MCE count $prev -> $mce at $(date -u -Is)"
      printf '%s\n' "$mce_out" | tail -20
    } > "$STATE/mce.latched"
  fi
  echo "$mce" > "$STATE/mce.count"
else
  # A failed query is NOT zero errors. Coercing it to 0 was how a broken
  # rasdaemon would have looked identical to healthy hardware.
  problems="${problems}MCE query FAILED (db and ras-mc-ctl) — cannot tell if MCEs occurred; "
fi
if [ -e "$STATE/mce.latched" ]; then
  problems="${problems}MACHINE CHECKS ($(head -1 "$STATE/mce.latched")) — ack with: rm $STATE/mce.latched; "
  critical="${critical}MCE: $(head -1 "$STATE/mce.latched"); "
fi

# 4. Container health — by IDENTITY and STATE, not just a count.
#
#    A bare threshold is close to useless here: with 37-39 services, Traefik
#    or a database can be down while 30+ containers keep the check green, and
#    a container stuck in a restart loop still counts as "running". Traefik
#    in particular is single point of failure for every routed service, so
#    "36 containers up" can mean "nothing is reachable".
#    ⚠️  RUNTIME-AGNOSTIC ON PURPOSE. During the k3s migration a service may
#    live in EITHER runtime, and after cutover docker is gone entirely. A
#    docker-only check does not fail when that happens — it reports zero
#    containers and stays "green" on a host where everything moved, or goes
#    permanently red for the wrong reason. Either way it stops being a
#    signal exactly when the migration makes it most needed.
#
#    This host is a k3s AGENT: no API server, no kubectl. `crictl` against
#    the node's own containerd is the only view of migrated workloads.
# ⛔ The docker half of this count was removed 2026-08-17 with the daemon. It read
# `dockern=$(docker ps -q | wc -l)` and the total was `dockern + k8sn`; with docker
# gone that term is permanently 0, and reporting "docker=0" in every alert is noise
# that trains you to skim the line.
k8sn=$(k3s crictl ps -q 2>/dev/null | wc -l)
running=$k8sn
# Threshold lowered 35 -> 28 on 2026-08-06: readarr was dropped entirely,
# host-hostnames removed as incompatible, and the three PinCollector
# containers deliberately stopped — 35 became 32 by intent, not by fault.
# A threshold that no longer matches reality either cries wolf or, once
# ignored, stops meaning anything. Re-raise it as services return.
if [ "$running" -lt 28 ]; then
  problems="${problems}only $running workloads running (k8s=$k8sn); "
fi

#    Named critical services. Losing any one of these is an outage no
#    count-based check would notice. Derived from the pre-shutdown inventory
#    at /storage2/safety/inventory/.
#
#    Checked in BOTH runtimes so a service can migrate without the check
#    either false-alarming or silently stopping to watch it.
#    infra-postgres-1 (PinCollector) was REMOVED from this list 2026-08-06:
#    the whole PinCollector stack — infra-api-1, infra-minio-1,
#    infra-postgres-1 — was deliberately stopped and is parked pending its
#    own migration decision (see docs/architecture/k3s.md backlog). Leaving it
#    here would page for an intentional state, and an alert that fires for
#    something you chose is how you learn to ignore the alert.
#    Re-add it when the stack comes back.
#    `traefik`, not `traefik2`: the ingress container was renamed 2026-08-07.
#    This watchlist matches by exact name, so leaving the old one here would
#    have paged CRITICAL on every single run from the moment of the rename —
#    for a container that is healthy and simply called something else.
#    `readmeabook` added 2026-08-08 when it migrated. It carries its own Postgres,
#    so losing it silently is a database outage, and the count check above has too
#    much slack to notice one missing workload.
#    `deluge-books` added 2026-08-09 when it migrated. ⚠️ The name here must match
#    the CONTAINER name in the Pod, not the Deployment name — this loop greps
#    crictl output for `"name": "<c>"`. The manifest names that container
#    `deluge-books` precisely so this entry matches.
# Authentik's initial foundation landed with all three controllers declaratively at
# zero. Do not page during a future intentionally inert restore, but once server +
# worker + database have all run together, latch that expectation permanently. A
# later crash, accidental scale-to-zero, or lost manifest then becomes loud instead
# of silently resetting the baseline.
authentik_marker="$STATE/authentik.expected"
if [ ! -e "$authentik_marker" ]; then
  authentik_all_running=1
  for c in authentik-server authentik-worker authentik-postgresql; do
    k3s crictl ps -o json 2>/dev/null \
      | grep -q "\"name\": \"$c\"" || authentik_all_running=0
  done
  if [ "$authentik_all_running" = 1 ]; then
    printf 'latched after all Authentik components ran together at %s\n' \
      "$(date -u +%FT%TZ)" > "$authentik_marker.tmp"
    mv "$authentik_marker.tmp" "$authentik_marker"
  fi
fi

critical_workloads="traefik nextcloud-db immich-postgres14 plex jellyfin immich nextcloud readmeabook tracearr deluge-books deluge-vpn qbittorrent-books flaresolverr-books nextcloud nextcloud-db"
if [ -e "$authentik_marker" ]; then
  critical_workloads="$critical_workloads authentik-server authentik-worker authentik-postgresql"
fi
for c in $critical_workloads; do
  # (the docker probe that used to lead here was removed 2026-08-17; crictl was
  # already the fallback and is now the only runtime)
  k3s crictl ps -o json 2>/dev/null \
    | grep -q "\"name\": \"$c\"" && continue
  problems="${problems}CRITICAL workload $c NOT running; "
done

#    ⛔ SYNTHETIC BRIDGE PROBES — the one thing docker-bridges.yaml has demanded
#    since it was written and nothing ever implemented.
#
#    A bridge is a selectorless Service plus a hand-written EndpointSlice pointing
#    at 10.0.1.6. Kubernetes keeps advertising that endpoint whether or not the
#    docker container behind it is running, crashed, or had its published port
#    changed — a Pod being Ready proves NOTHING about the far side. The consumer
#    just gets connection refused, and nothing anywhere says why.
#
#    So probe the EXACT host:port each EndpointSlice advertises. These are the
#    real published ports, which deliberately differ from the Service ports their
#    consumers use (gluetun 8080->8880, deluge-books 8112->9812).
#
#    Failure is reported, not fatal: these back torrent clients, so a dropped VPN
#    should be loud without turning the whole heartbeat red for an hour.
#    `deluge-books:9812` REMOVED 2026-08-09 — it migrated to k3s, its bridge
#    EndpointSlice was pruned, and nothing listens on 9812 any more. Leaving it
#    would report a dead bridge on every run, forever.
#    `deluge-vpn:8112` REMOVED 2026-08-09 — migrated, its bridge EndpointSlice
#    pruned, and nothing listens on the host port any more.
#    ⛔ ALL BRIDGE PROBES REMOVED 2026-08-09 — every bridged service has migrated.
#    The loop is kept (not deleted) because the NEXT docker service to be bridged
#    should re-add its host:port here rather than reinvent the probe.
for bp in ; do
  bn=${bp%%:*}; bport=${bp##*:}
  curl -fsS -o /dev/null -m 10 "http://10.0.1.6:$bport/" 2>/dev/null && continue
  # A 401/403 is a HEALTHY answer from an authenticated UI — curl -f treats it as
  # failure, so re-check for any HTTP response at all before complaining.
  code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "http://10.0.1.6:$bport/" 2>/dev/null || echo 000)
  [ "$code" != "000" ] && continue
  problems="${problems}BRIDGE DEAD: $bn advertises 10.0.1.6:$bport but nothing answers; "
done

#    Restart loops present as "running" to a counter. Catch them explicitly.
# ⛔ `docker ps --filter status=restarting` was removed 2026-08-17 with the daemon.
# There is no crictl equivalent: containerd has no "restarting" state, because the
# kubelet -- not the runtime -- owns restart backoff. The attempt-delta check below
# is the replacement and is strictly better anyway: it catches a crash loop whatever
# state the container is caught in, which is the reason that check was added.

#    ...but `status=restarting` is NOT sufficient, and this was proven the
#    expensive way. palworld-server crash-looped for EIGHT DAYS — 753 restarts,
#    a corrupted save, ~700 GB/hour of pointless steamcmd re-verification —
#    and neither the count check nor the line above ever fired.
#
#    Why: `restarting` is the brief back-off state between attempts. palworld
#    spent ~25 s per cycle actually running (verifying 5 GB, loading, crashing),
#    so it was in `running` state well over 95% of the time. To a counter it was
#    simply one of 32 healthy containers. The unhealthy check below missed it
#    too, for a different reason: a container that keeps dying inside its
#    start-period reads as health `starting`, never reaching `unhealthy`, and
#    `--filter health=unhealthy` does not match `starting`.
#
#    The signal that cannot be missed is RestartCount GROWTH. A container that
#    restarts repeatedly between two pings is crash-looping, whatever state it
#    happens to be caught in.
prev=/var/lib/healthcheck-ping/restart-counts
cur=$(mktemp)
# ⛔ PORTED from docker to crictl, 2026-08-17. This was
#     for c in $(docker ps -a --format '{{.Names}}'); do
#       n=$(docker inspect -f '{{.RestartCount}}' "$c")
# and removing it with the daemon would have silently deleted crash-loop detection.
#
# containerd's equivalent of RestartCount is `metadata.attempt`, which the kubelet
# increments each time it recreates a container. Same delta logic, same threshold.
#
# ⚠️ ONE LINE PER NAME, taking the HIGHEST attempt. `crictl ps -a` lists every
# attempt of a container as its own row, so the same name appears repeatedly at
# different attempt numbers -- measured on the live host: `verify-pgdata 118` and
# `verify-pgdata 117` side by side. Docker's container names were unique, so the
# original code had no such case. Without the group_by/max_by the delta would be
# computed against whichever duplicate happened to be read first, which is both
# wrong and unstable between runs.
#
# ⚠️ Parsed as JSON with jq, NOT from the `crictl ps` table. `crictl ps` has no
# --template (only inspect/inspectp do), and its CREATED column is variable-width
# prose -- column-position parsing there is exactly the bug that made this heartbeat
# alert on a pod named "Ready" until 2026-08-16.
while read -r c n; do
  [ -z "$c" ] && continue
  echo "$c $n" >> "$cur"
done <<EOF
$(k3s crictl ps -a -o json 2>/dev/null \
   | jq -r '[.containers[]? | {n: .metadata.name, a: (.metadata.attempt // 0)}]
            | group_by(.n)[] | max_by(.a) | "\(.n) \(.a)"' 2>/dev/null)
EOF
if [ -f "$prev" ]; then
  looping=""
  while read -r c n; do
    o=$(awk -v k="$c" '$1==k {print $2; exit}' "$prev" 2>/dev/null)
    [ -z "$o" ] && continue
    d=$(( n - o ))
    # >=3 between pings, so a single deliberate restart never pages. A real
    # crash loop produces dozens.
    [ "$d" -ge 3 ] && looping="$looping$c(+$d) "
  done < "$cur"
  [ -n "$looping" ] && problems="${problems}CRASH LOOPING: $looping; "
fi
mv "$cur" "$prev" 2>/dev/null || rm -f "$cur"

#    k8s sandbox state. This does NOT expose Kubernetes container readiness:
#    a sandbox remains Ready when gluetun's readinessProbe fails, so the two
#    VPN tunnels need a separate behavioral check below.
#
# ⛔ DO NOT go back to parsing `crictl pods` by column position. The previous
# implementation was:
#
#     k3s crictl pods | awk 'NR>1 && $5!="Ready" {print $6}'
#
# and it had TWO defects, both live on 2026-08-16:
#
#  1. CREATED is human-readable text of VARIABLE WIDTH. "2 hours ago" is three
#     fields but "About an hour ago" is four, so for any pod in its second hour
#     $5 was "ago" rather than the state. Healthy pods were reported unhealthy,
#     and $6 was then the literal word "Ready" -- the heartbeat was alerting on a
#     pod named "Ready". Verified against live output, not inferred.
#  2. A Job's sandbox is SUPPOSED to end NotReady. jellyfin-quiesce and
#     pin-collector-migrate are `Completed` and were counted as failures, which is
#     three false positives every run, drowning real signals.
#
# `--state notready -q` filters server-side and returns IDs, so there are no
# columns to mis-parse. Names and labels come from go-template, the same
# mechanism the tunnel probe below already relies on.
k8s_unhealthy_pods() {
  out="" pid="" name="" job="" bad="" seen="" cid="" code=""
  for pid in $(k3s crictl pods --state notready -q 2>/dev/null); do
    name=$(k3s crictl inspectp -o go-template \
      --template '{{.status.metadata.name}}' "$pid" 2>/dev/null)
    [ -n "$name" ] || name="$pid"

    job=$(k3s crictl inspectp -o go-template \
      --template '{{index .status.labels "job-name"}}' "$pid" 2>/dev/null)

    # go-template prints the literal string `<no value>` for an absent label.
    if [ -n "$job" ] && [ "$job" != "<no value>" ]; then
      # It is a Job pod, so NotReady is expected. Only a BAD ending is a problem.
      bad=0
      seen=0
      for cid in $(k3s crictl ps -a --pod "$pid" -q 2>/dev/null); do
        seen=$((seen + 1))
        code=$(k3s crictl inspect "$cid" 2>/dev/null \
          | awk -F: '/"exitCode"/ { gsub(/[ ,]/, "", $2); print $2; exit }')
        # An unreadable exit code is NOT evidence of success. This program has no
        # `set -e`, so a failed inspect would otherwise vanish silently -- which is
        # exactly how the missing-gawk bug hid for as long as it did.
        [ "${code:-1}" = "0" ] || bad=1
      done
      # A Job sandbox with no containers is not proof it succeeded either.
      [ "$seen" -eq 0 ] && bad=1
      [ "$bad" -eq 0 ] && continue
      name="$name(job-failed)"
    fi

    out="$out$name "
  done
  printf '%s' "$out"
}

notready=$(k8s_unhealthy_pods)
[ -n "$notready" ] && problems="${problems}k8s pods NOT Ready: $notready; "

#    Test actual public egress from exactly the two Deluge Pod network
#    namespaces. The request uses a literal IP, so host DNS cannot make a
#    broken tunnel look healthy, and all response/error output is suppressed.
#    Each probe is hard-bounded to 3s + 3s + 9s (including SIGKILL grace)
#    and both run in parallel: this block has a 15s ceiling, within the
#    unit's 60s TimeoutStartSec rather than consuming it once per tunnel.
tunnel_probe() {
  tunnel="$1"
  result="$2"
  pod_ids=$(timeout --kill-after=1s 2s k3s crictl pods -q --namespace media \
    --name "^$tunnel-[a-z0-9]+-[a-z0-9]+$" --state ready 2>/dev/null)
  # shellcheck disable=SC2086  # word splitting is the POINT here.
  # crictl returns zero or more whitespace-separated pod IDs. Splitting them into
  # positional parameters is how the count below is taken; quoting would collapse
  # every result into one argument and the `-ne 1` test would stop distinguishing
  # 'exactly one ready sandbox' from 'three of them'.
  set -- $pod_ids
  if [ "$#" -ne 1 ]; then
    printf '%s\n' "TUNNEL $tunnel FAILED (expected one ready pod sandbox, found $#)" > "$result"
    return
  fi
  sandbox_pid=$(timeout --kill-after=1s 2s k3s crictl inspectp -o go-template \
    --template '{{.info.pid}}' "$1" 2>/dev/null)
  case "$sandbox_pid" in
    *[!0-9]*|"")
      printf '%s\n' "TUNNEL $tunnel FAILED (pod network namespace unavailable)" > "$result"
      return
      ;;
  esac
  if ! timeout --kill-after=1s 8s nsenter --target "$sandbox_pid" --net \
    curl -fsS --connect-timeout 3 --max-time 7 -o /dev/null \
      https://1.1.1.1/cdn-cgi/trace >/dev/null 2>&1; then
    printf '%s\n' "TUNNEL $tunnel FAILED (no public connectivity from pod network namespace)" > "$result"
    return
  fi
  : > "$result"
}

if tunnel_results=$(mktemp -d); then
  tunnel_probe deluge-vpn "$tunnel_results/deluge-vpn" &
  tunnel_probe deluge-books "$tunnel_results/deluge-books" &
  wait
  for tunnel in deluge-vpn deluge-books; do
    if [ -s "$tunnel_results/$tunnel" ]; then
      problems="${problems}$(cat "$tunnel_results/$tunnel"); "
    fi
  done
  rm -r "$tunnel_results"
else
  problems="${problems}TUNNEL checks FAILED (cannot create result directory); "
fi

#    Unhealthy, excluding two that were already unhealthy BEFORE the
#    migration (nextcloud-redis, deluge-books) — see docs/runbooks/minas-tirith/backup-restore.md. Alerting
#    on known-pre-existing state would train you to ignore this line.
# ⛔ `docker ps --filter health=unhealthy` was removed 2026-08-17 with the daemon.
#
# ⚠️ THIS IS A COVERAGE LOSS, recorded rather than glossed. Docker HEALTHCHECK has
# no containerd equivalent -- in Kubernetes the readiness probe plays that role, and
# the kubelet acts on it by removing the Pod from Service endpoints rather than by
# exposing an "unhealthy" state a runtime query can see.
#
# The nearest replacement already runs above: k8s_unhealthy_pods(). It is NOT
# equivalent -- it sees sandbox state, not per-container readiness, so a Pod whose
# readinessProbe is failing still looks Ready to it. That gap is why the two VPN
# tunnels have their own behavioural probe. See ROADMAP item 3.
# 5. SMART. smartd logs, but logs on a remote box nobody reads are not
#    monitoring. /dev/sdc already carries 24 pending + 9 offline
#    uncorrectable sectors AND sits inside the raidz2, so its decline is
#    the single most likely next hardware event.
#
#    Alarm ONLY on an explicit failure verdict. The nine pool disks sit
#    behind an Adaptec HBA (aacraid), where smartctl may need `-d sat` and
#    can otherwise exit non-zero simply because it could not talk to the
#    device. Treating "couldn't query" as "disk failing" would fire nine
#    false alarms on every single ping — see the delta note above for why
#    that is worse than no check. Unreadable devices are counted and
#    reported once, quietly, rather than raised as failures.
unreadable=""; nunread=0; ntotal=0
for d in /dev/nvme0n1 /dev/sd?; do
  [ -e "$d" ] || continue
  ntotal=$((ntotal + 1))
  out=$(smartctl -H "$d" 2>/dev/null) \
    || out=$(smartctl -H -d sat "$d" 2>/dev/null) \
    || out=""
  if [ -z "$out" ]; then
    unreadable="$unreadable $d"; nunread=$((nunread + 1))
  elif echo "$out" | grep -qiE "FAILED!|FAILING_NOW|result: FAILED"; then
    problems="${problems}SMART health FAILED on $d; "
  fi
done
# 5a. SMART ATTRIBUTE DRIFT — separate from the -H verdict above, and the
#     one that actually matters here. `smartctl -H` reports the drive's own
#     boolean self-assessment, which stays "PASSED" while reallocation
#     counters climb. /dev/sdc ALREADY carries 24 Current_Pending_Sector +
#     9 Offline_Uncorrectable and sits inside the raidz2, so it is the most
#     likely next hardware event on this machine — and it would sail past a
#     PASSED/FAILED check the entire way down.
#
#     Delta against a stored baseline, because absolute counts would alarm
#     forever on sdc's existing 33. Growth is the signal; standing damage
#     is already known and recorded in the runbooks.
#     Keyed by SERIAL, not /dev/sdX. Kernel device names are not stable —
#     this board renumbers PCI devices (hence pci=realloc=off) and sd*
#     ordering depends on discovery order. Keying by sdX would silently
#     compare one disk's baseline against another's counters after a
#     reshuffle, producing both false alarms and false silence.
#
#     Growth LATCHES, exactly like MCEs above. The previous version wrote
#     the new baseline immediately, so if the ping failed to deliver the
#     next run compared 27 against 27 and reported nothing — recreating the
#     dropped-event bug that latching was introduced to kill.
for d in /dev/nvme0n1 /dev/sd?; do
  [ -e "$d" ] || continue
  # Capture attempts SEPARATELY. `a || b` with both appended would sum a
  # doubled attribute table when the first call prints a valid table but
  # exits non-zero (smartctl uses exit BITS, e.g. 16, for transport state).
  att=$(smartctl -A "$d" 2>/dev/null || true)
  if ! printf '%s\n' "$att" | grep -qE "Current_Pending_Sector|Media and Data Integrity"; then
    att=$(smartctl -A -d sat "$d" 2>/dev/null || true)
  fi
  # Require RECOGNISED rows. An error message is non-empty output too, and
  # treating "no rows parsed" as a counter of 0 would read as perfect
  # health on a disk we cannot actually see.
  if ! printf '%s\n' "$att" | grep -qE "Current_Pending_Sector|Media and Data Integrity"; then
    continue
  fi
  key=$(smartctl -i "$d" 2>/dev/null \
        | grep -iE "^Serial Number:" | awk '{print $3}')
  [ -z "$key" ] && key=$(basename "$d")
  bad=$(printf '%s\n' "$att" \
        | awk '
            /Current_Pending_Sector|Offline_Uncorrectable|Reallocated_Sector_Ct/ { s += $10 }
            /^Media and Data Integrity Errors:/ { gsub(/,/,"",$6); s += $6 }
            END { print s + 0 }')
  # FIRST observation is a commissioning BASELINE, never "growth". On a
  # fresh root there is no state file, and /dev/sdc legitimately carries 33
  # pending+uncorrectable sectors — comparing that against an implicit 0
  # would latch a permanent critical on day one, hold the dedicated check
  # red forever, and mask the real growth this check exists to catch.
  if [ ! -r "$STATE/smart.$key" ]; then
    echo "$bad" > "$STATE/smart.$key"
    echo "SMART baseline for $key ($d): $bad"
    continue
  fi
  pb=$(cat "$STATE/smart.$key")
  case "$pb" in *[!0-9]*|"") pb=0 ;; esac
  if [ "$bad" -gt "$pb" ]; then
    echo "$key ($d) SMART $pb -> $bad at $(date -u -Is)" > "$STATE/smartgrow.$key"
  fi
  echo "$bad" > "$STATE/smart.$key"
done
for f in "$STATE"/smartgrow.*; do
  [ -e "$f" ] || continue
  problems="${problems}DISK DEGRADING ($(cat "$f")) — ack with: rm $f; "
  critical="${critical}DISK: $(cat "$f"); "
done

# Losing SMART visibility entirely must be reported, not just logged.
# Previously this only went to stderr, so if the aacraid addressing is
# wrong (still UNVERIFIED on the live HBA — it may need -d aacraid,H,L,ID)
# then nine unmonitored disks would look exactly like nine healthy disks
# to anyone reading the heartbeat. Silence is not health.
if [ "$ntotal" -gt 0 ] && [ "$nunread" -eq "$ntotal" ]; then
  problems="${problems}SMART UNREADABLE on ALL $ntotal disks — no disk monitoring at all; "
elif [ "$nunread" -gt 0 ]; then
  problems="${problems}SMART unreadable on $nunread of $ntotal disks ($unreadable ); "
fi

# 5b. Is there actually a watchdog? `sp5100_tco` may lose the hardware to
#     the BMC and refuse to bind, in which case systemd runs watchdog-less
#     and SILENTLY — the protection against the exact hard hangs this
#     machine has taken would simply not exist. See ./backup-root-data.nix.
if ! ls /dev/watchdog* >/dev/null 2>&1; then
  problems="${problems}NO /dev/watchdog — hung-kernel auto-reboot is NOT active; "
fi

# 5c. Is the nightly backup actually running? ext4 does not checksum data,
#     so this backup IS the compensating control. A backup that quietly
#     stopped is the failure mode that only reveals itself when you need
#     to restore.
#
#     A MISSING stamp is UNHEALTHY, not healthy. The earlier version only
#     checked staleness when the stamp existed, so a backup that had never
#     succeeded even once reported green forever — the single worst case,
#     silently indistinguishable from a working one.
#
#     The failure marker exists because backup failures were otherwise
#     erased: `notify-failure@` pings /fail, and then this heartbeat pings
#     success up to five minutes later on the same check, clearing it.
#     Failure state has to persist until a backup actually succeeds, so it
#     lives on disk rather than in a one-shot ping.
# A DEGRADED backup ran and copied files, but at least one database dump
# failed — meaning the only application-consistent copy of that database is
# stale. The file-level copy still exists, so this is not a failure, but it
# must not read as fully healthy either: a hot-copied PostgreSQL directory
# may not restore at all.
if [ -e /var/lib/backup-root-data.retention-failed ]; then
  problems="${problems}$(cat /var/lib/backup-root-data.retention-failed 2>/dev/null); "
fi
if [ -e /var/lib/backup-root-data.degraded ]; then
  problems="${problems}$(cat /var/lib/backup-root-data.degraded 2>/dev/null); "
fi
if [ -e /var/lib/backup-root-data.failed ]; then
  problems="${problems}last backup FAILED ($(cat /var/lib/backup-root-data.failed 2>/dev/null || echo unknown)); "
elif [ -r /var/lib/backup-root-data.stamp ]; then
  stamp=$(cat /var/lib/backup-root-data.stamp)
  case "$stamp" in *[!0-9]*|"") stamp=0 ;; esac
  age=$(( $(date -u +%s) - stamp ))
  if [ "$age" -gt 172800 ]; then
    problems="${problems}backup last succeeded $((age / 86400))d ago; "
  fi
else
  problems="${problems}backup has NEVER succeeded (no stamp); "
fi

# 6. Did sops actually decrypt? If the SSH host keys were not restored
#    correctly, sshd silently generates new ones, sops cannot decrypt, and
#    the console password never materialises — leaving the machine
#    reachable only by SSH key. That is a silent single point of failure,
#    so surface it.
if [ ! -s /run/secrets-for-users/edgar-password ]; then
  problems="${problems}sops secret missing: no console password, SSH-key access only; "
fi

# The dashboard is itself monitoring infrastructure. Scrutiny's health
# endpoint checks both its SQLite inventory and InfluxDB history, so fold
# it into the existing heartbeat rather than letting the fleet view die
# silently while its configuration still looks correct.
if ! curl -fsS -m 5 http://127.0.0.1:9080/api/health \
  | grep -q '"success":true'; then
  problems="${problems}Scrutiny dashboard/database health check FAILED; "
fi

# Scrutiny's notification script writes this before attempting the
# outbound critical ping. Consequently a lost outbound notification is
# still surfaced by every aggregate heartbeat until a human explicitly
# acknowledges it. See docs/operations/disk-health.md for the acknowledgement command.
if [ -r /var/lib/scrutiny/alert.latched ]; then
  scrutiny_alert=$(head -1 /var/lib/scrutiny/alert.latched)
  problems="${problems}SCRUTINY DISK ALERT ($scrutiny_alert) — explicit acknowledgement required; "
  critical="${critical}SCRUTINY: $scrutiny_alert; "
fi

# Deliver. Curl failure is deliberately non-fatal — a dead monitoring
# endpoint must never wedge the box — and it no longer needs to gate any
# state, because hardware errors now LATCH on disk (above) rather than
# relying on a single ping being both sent and noticed.
#
# This matters more than it looks: Healthchecks alerts on state
# TRANSITION, so if the check is already Down for an unrelated reason
# (SMART blind, backup failed), a fresh /fail may produce no new
# notification at all. Anything that must not be missed therefore has to
# survive locally until a human clears it, not depend on delivery.
if [ -n "$problems" ]; then
  curl -fsS -m 20 --data-raw "UNHEALTHY: $problems" "$URL/fail" \
    || echo "WARNING: failure ping did not deliver" >&2
else
  curl -fsS -m 20 "$URL" || echo "WARNING: success ping did not deliver" >&2
fi

# Critical hardware events go to their own check as well, so they are not
# buried inside an aggregate that may already be red for softer reasons.
if [ "$CRIT" != "$URL" ]; then
  if [ -n "$critical" ]; then
    curl -fsS -m 20 --data-raw "CRITICAL: $critical" "$CRIT/fail" \
      || echo "WARNING: critical ping did not deliver" >&2
  else
    curl -fsS -m 20 "$CRIT" || echo "WARNING: critical-ok ping did not deliver" >&2
  fi
fi
exit 0
