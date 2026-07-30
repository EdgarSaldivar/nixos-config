# minas-tirith — outward monitoring.
#
# The old host had none. Consequences, all observed in July 2026:
#   - a multi-hour outage was discovered by hand, by trying to use the machine
#   - filesystem corruption sat unnoticed between monthly scrubs
#   - a fatal machine check went unrecorded entirely
#
# This is a health-GATED heartbeat, not a liveness ping. A bare "I'm alive"
# curl would have stayed green through the read-only filesystem failure on
# 2026-07-29 — the box was up and serving reads the whole time. This one checks
# the things that actually went wrong and reports failure explicitly.
{ config, pkgs, ... }:
{
  # The ping URL is a secret in THIS repo specifically: nixos-config is a PUBLIC
  # GitHub repository. Anyone with the URL can ping it and thereby suppress a
  # genuine outage alert — the monitoring would look healthy while the machine
  # was down. Kept in sops, read at runtime, never in the store or in git.
  sops.secrets.healthchecks-url = { };

  systemd.services.healthcheck-ping = {
    description = "Health-gated heartbeat to healthchecks.io";
    path = with pkgs; [
      curl
      util-linux
      zfs
      docker
      smartmontools
      gnugrep
      coreutils
    ];
    serviceConfig = {
      Type = "oneshot";
      # Never let the heartbeat itself be the thing that wedges the box.
      TimeoutStartSec = "60s";
      # Persistent state for delta comparisons -> /var/lib/healthcheck-ping
      StateDirectory = "healthcheck-ping";
    };
    script = ''
      URL="$(cat ${config.sops.secrets.healthchecks-url.path})"
      STATE="''${STATE_DIRECTORY:-/var/lib/healthcheck-ping}"
      problems=""

      # 1. Is the root filesystem still writable? This is exactly what failed
      #    on 2026-07-29 while everything else looked healthy.
      if ! findmnt -no OPTIONS / | grep -qw rw; then
        problems="''${problems}root filesystem is READ-ONLY; "
      fi

      # 2. Both ZFS pools healthy? (98 TB lives here)
      pools=$(zpool status -x 2>/dev/null || echo "zpool status failed")
      if ! echo "$pools" | grep -q "all pools are healthy"; then
        problems="''${problems}zpool: $(echo "$pools" | head -1); "
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
      if ! ${pkgs.systemd}/bin/systemctl is-active --quiet rasdaemon; then
        problems="''${problems}rasdaemon NOT running — MCE detection is BLIND; "
      fi
      if mce_out=$(${pkgs.rasdaemon}/bin/ras-mc-ctl --errors 2>/dev/null); then
        mce=$(printf '%s\n' "$mce_out" | grep -cE "^[0-9]+ [0-9]{4}-[0-9]{2}-[0-9]{2}" || true)
        mce=''${mce:-0}
        prev=0
        if [ -r "$STATE/mce.count" ]; then
          prev=$(cat "$STATE/mce.count")
          case "$prev" in *[!0-9]*|"") prev=0 ;; esac
        fi
        if [ "$mce" -gt "$prev" ]; then
          {
            echo "count $prev -> $mce at $(date -u -Is)"
            printf '%s\n' "$mce_out" | grep -E "^[0-9]+ [0-9]{4}-" | tail -20
          } > "$STATE/mce.latched"
        fi
        echo "$mce" > "$STATE/mce.count"
      else
        # A failed query is NOT zero errors. Coercing it to 0 was how a broken
        # rasdaemon would have looked identical to healthy hardware.
        problems="''${problems}ras-mc-ctl query FAILED — cannot tell if MCEs occurred; "
      fi
      if [ -e "$STATE/mce.latched" ]; then
        problems="''${problems}HARDWARE ERRORS ($(head -1 "$STATE/mce.latched")) — ack with: rm $STATE/mce.latched; "
      fi

      # 4. Did we lose a chunk of the container stack?
      running=$(docker ps -q 2>/dev/null | wc -l)
      if [ "$running" -lt 30 ]; then
        problems="''${problems}only $running containers running; "
      fi

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
        out=$(${pkgs.smartmontools}/bin/smartctl -H "$d" 2>/dev/null) \
          || out=$(${pkgs.smartmontools}/bin/smartctl -H -d sat "$d" 2>/dev/null) \
          || out=""
        if [ -z "$out" ]; then
          unreadable="$unreadable $d"; nunread=$((nunread + 1))
        elif echo "$out" | grep -qiE "FAILED!|FAILING_NOW|result: FAILED"; then
          problems="''${problems}SMART health FAILED on $d; "
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
      for d in /dev/nvme0n1 /dev/sd?; do
        [ -e "$d" ] || continue
        key=$(basename "$d")
        att=$(${pkgs.smartmontools}/bin/smartctl -A "$d" 2>/dev/null \
              || ${pkgs.smartmontools}/bin/smartctl -A -d sat "$d" 2>/dev/null || true)
        [ -z "$att" ] && continue
        bad=$(printf '%s\n' "$att" \
              | ${pkgs.gawk}/bin/awk '
                  /Current_Pending_Sector|Offline_Uncorrectable|Reallocated_Sector_Ct/ { s += $10 }
                  /^Media and Data Integrity Errors:/ { gsub(/,/,"",$6); s += $6 }
                  END { print s + 0 }')
        pb=0
        if [ -r "$STATE/smart.$key" ]; then
          pb=$(cat "$STATE/smart.$key")
          case "$pb" in *[!0-9]*|"") pb=0 ;; esac
        fi
        if [ "$bad" -gt "$pb" ]; then
          problems="''${problems}$key SMART counters GREW $pb -> $bad (pending/uncorrectable/reallocated); "
        fi
        echo "$bad" > "$STATE/smart.$key"
      done

      # Losing SMART visibility entirely must be reported, not just logged.
      # Previously this only went to stderr, so if the aacraid addressing is
      # wrong (still UNVERIFIED on the live HBA — it may need -d aacraid,H,L,ID)
      # then nine unmonitored disks would look exactly like nine healthy disks
      # to anyone reading the heartbeat. Silence is not health.
      if [ "$ntotal" -gt 0 ] && [ "$nunread" -eq "$ntotal" ]; then
        problems="''${problems}SMART UNREADABLE on ALL $ntotal disks — no disk monitoring at all; "
      elif [ "$nunread" -gt 0 ]; then
        problems="''${problems}SMART unreadable on $nunread of $ntotal disks ($unreadable ); "
      fi

      # 5b. Is there actually a watchdog? `sp5100_tco` may lose the hardware to
      #     the BMC and refuse to bind, in which case systemd runs watchdog-less
      #     and SILENTLY — the protection against the exact hard hangs this
      #     machine has taken would simply not exist. See ./system.nix.
      if ! ls /dev/watchdog* >/dev/null 2>&1; then
        problems="''${problems}NO /dev/watchdog — hung-kernel auto-reboot is NOT active; "
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
      if [ -e /var/lib/backup-root-data.failed ]; then
        problems="''${problems}last backup FAILED ($(cat /var/lib/backup-root-data.failed 2>/dev/null || echo unknown)); "
      elif [ -r /var/lib/backup-root-data.stamp ]; then
        stamp=$(cat /var/lib/backup-root-data.stamp)
        case "$stamp" in *[!0-9]*|"") stamp=0 ;; esac
        age=$(( $(date -u +%s) - stamp ))
        if [ "$age" -gt 172800 ]; then
          problems="''${problems}backup last succeeded $((age / 86400))d ago; "
        fi
      else
        problems="''${problems}backup has NEVER succeeded (no stamp); "
      fi

      # 6. Did sops actually decrypt? If the SSH host keys were not restored
      #    correctly, sshd silently generates new ones, sops cannot decrypt, and
      #    the console password never materialises — leaving the machine
      #    reachable only by SSH key. That is a silent single point of failure,
      #    so surface it.
      if [ ! -s /run/secrets-for-users/edgar-password ]; then
        problems="''${problems}sops secret missing: no console password, SSH-key access only; "
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
      exit 0
    '';
  };

  systemd.timers.healthcheck-ping = {
    description = "Send heartbeat every 5 minutes";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "2min";
      OnUnitActiveSec = "5min";
      AccuracySec = "30s";
    };
  };
}
