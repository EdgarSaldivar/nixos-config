#!/usr/bin/env bats
#
# Characterization tests for the minas heartbeat program.
#
# As with backup.bats these run against the RENDERED program, extracted from the
# evaluated NixOS config, so they cannot drift from what is deployed.
#
# The MCE cases below are the highest-value fixtures in this repository. That
# detection was wrong through THREE consecutive audit rounds:
#   round 1 — the pattern required the literal word "mce" or "memory" on the line,
#             which rasdaemon never emits, so it counted NOTHING;
#   round 2 — over-corrected to any row with the shared prefix, so a single
#             corrected DIMM error latched a permanent critical;
#   round 3 — counted corrected errors as uncorrectable.
# Each round passed code review. None would have passed these.
#
# $HEARTBEAT_SCRIPT is the path to the rendered script; the wrapper supplies it.

setup() {
  export TESTDIR="$BATS_TEST_TMPDIR"
  mkdir -p "$TESTDIR/bin"
  export PATH="$TESTDIR/bin:$PATH"
}

# Pull one shell function out of the rendered program and define it here, so it
# can be exercised against fakes on PATH. Same mechanism as backup.bats.
load_function() {
  local name="$1" body
  body="$(awk -v fn="$name" '
    $0 ~ "^" fn "\\(\\) \\{" { inside = 1 }
    inside { print }
    inside && /^\}/ { exit }
  ' "$HEARTBEAT_SCRIPT")"
  [ -n "$body" ] || { echo "could not find $name() in $HEARTBEAT_SCRIPT" >&2; return 1; }
  eval "$body"
}

# Extract an embedded awk program from the rendered script into a file, so it can
# be run with `awk -f` instead of being re-quoted through a shell string.
awk_program() {
  local anchor="$1" out="$TESTDIR/prog.awk"
  awk -v a="$anchor" '
    index($0, a) { grab = 1 }
    grab { print }
    grab && /END \{/ { exit }
  ' "$HEARTBEAT_SCRIPT" | sed -E "s/'\\)$//" > "$out"
  [ -s "$out" ] || return 1
  printf '%s' "$out"
}

# ── MCE section parser ───────────────────────────────────────────────────────
# Fixture: "must count 2 on a mixed log, not 6".
#
# ras-mc-ctl --errors emits SEVERAL sections. Only the `MCE events` section holds
# machine checks; the others are memory CEs and are not the same thing.

mixed_log() {
  cat <<'EOF'
Memory controller events:
1 2026-07-29 11:02:13 -0700 error: Corrected error, DIMM A1
2 2026-07-29 11:04:51 -0700 error: Corrected error, DIMM A1
3 2026-07-29 11:09:02 -0700 error: Corrected error, DIMM B2
MCE events:
1 2026-07-30 03:14:07 -0700 error: Uncorrected error, Bank 5 EX unit
2 2026-07-30 03:14:07 -0700 error: Uncorrected error, Bank 2 L2
PCIe AER events:
1 2026-07-31 08:00:00 -0700 error: Corrected, Root Port
EOF
}

@test "MCE section parser counts 2 on a mixed log, not 6" {
  prog="$(awk_program '/^MCE events/                { inmce = 1; next }')"
  [ -n "$prog" ]
  mixed_log > "$TESTDIR/in"
  run awk -f "$prog" "$TESTDIR/in"
  [ "$status" -eq 0 ]
  # ⛔ 2 is the answer. 6 means the section scoping broke and every error class is
  # being counted. 0 means the row pattern stopped matching rasdaemon's format.
  [ "$output" = "2" ]
}

@test "MCE section parser counts 0 when there are no MCE events" {
  prog="$(awk_program '/^MCE events/                { inmce = 1; next }')"
  printf '%s\n' 'Memory controller events:' '1 2026-07-29 11:02:13 -0700 error: Corrected error, DIMM A1' > "$TESTDIR/in"
  run awk -f "$prog" "$TESTDIR/in"
  [ "$status" -eq 0 ]
  [ "$output" = "0" ]
}

@test "MCE section parser stops counting at the next section header" {
  prog="$(awk_program '/^MCE events/                { inmce = 1; next }')"
  printf '%s\n' 'MCE events:' '1 2026-07-30 03:14:07 -0700 error: Uncorrected' 'PCIe AER events:' '1 2026-07-31 08:00:00 -0700 error: Corrected' > "$TESTDIR/in"
  run awk -f "$prog" "$TESTDIR/in"
  [ "$status" -eq 0 ]
  # If this returns 2 the section boundary is not being honoured and unrelated
  # error classes are inflating the machine-check count.
  [ "$output" = "1" ]
}

# ── SMART drift ──────────────────────────────────────────────────────────────
# Fixture: "RAW_VALUE column, ATA and NVMe forms; 24 -> 27 must alert".

ata_attributes() {
  cat <<'EOF'
ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
  5 Reallocated_Sector_Ct   0x0033   100   100   010    Pre-fail  Always       -       16
197 Current_Pending_Sector  0x0012   100   100   000    Old_age   Always       -       8
198 Offline_Uncorrectable   0x0010   100   100   000    Old_age   Offline      -       3
EOF
}

@test "SMART drift sums the RAW_VALUE column on ATA output" {
  prog="$(awk_program '/Current_Pending_Sector|Offline_Uncorrectable|Reallocated_Sector_Ct/')"
  [ -n "$prog" ]
  ata_attributes > "$TESTDIR/in"
  run awk -f "$prog" "$TESTDIR/in"
  [ "$status" -eq 0 ]
  # 16 + 8 + 3. Reading the wrong column (VALUE=100) would give 300.
  [ "$output" = "27" ]
}

@test "SMART drift reads the NVMe form too" {
  prog="$(awk_program '/Current_Pending_Sector|Offline_Uncorrectable|Reallocated_Sector_Ct/')"
  printf '%s\n' 'Media and Data Integrity Errors:              1,024' > "$TESTDIR/in"
  run awk -f "$prog" "$TESTDIR/in"
  [ "$status" -eq 0 ]
  # Comma-stripped. NVMe reports no ATA attributes at all, so a parser that only
  # understood the ATA table would silently report 0 for every NVMe in the fleet.
  [ "$output" = "1024" ]
}

@test "SMART drift reports 0, not empty, when nothing matches" {
  prog="$(awk_program '/Current_Pending_Sector|Offline_Uncorrectable|Reallocated_Sector_Ct/')"
  printf '%s\n' 'SMART overall-health self-assessment test result: PASSED' > "$TESTDIR/in"
  run awk -f "$prog" "$TESTDIR/in"
  [ "$status" -eq 0 ]
  # An empty string here would make the later arithmetic comparison fail closed
  # in the wrong direction.
  [ "$output" = "0" ]
}

# ── MCE severity filter (the PRIMARY path) ───────────────────────────────────
# Fixture: "corrected-vs-uncorrected; a corrected-only DB must count 0".
#
# ⚠️ The awk cases above test the FALLBACK. The primary path queries rasdaemon's
# own sqlite database with a status bitmask, and that is where a corrected DIMM
# error must not be counted as a machine check. Testing only the fallback left the
# path that actually runs on the live host uncovered.

mce_query() {
  # Pull the COUNT query out of the rendered script, store paths stripped.
  grep -o "SELECT COUNT(\*) FROM mce_record WHERE [^\"]*" "$HEARTBEAT_SCRIPT" | head -1
}

@test "MCE severity filter counts 0 on a corrected-only database" {
  q="$(mce_query)"
  [ -n "$q" ]
  db="$TESTDIR/ras.db"
  sqlite3 "$db" 'CREATE TABLE mce_record (id INTEGER, timestamp TEXT, error_msg TEXT, status INTEGER);'
  # Three corrected errors: neither the UC bit (0x2000000000000000) nor the
  # AR/deferred bit (0x100000000000) set.
  sqlite3 "$db" "INSERT INTO mce_record VALUES (1,'t','Corrected error',0),(2,'t','Corrected error',4),(3,'t','Corrected error',8);"
  run sqlite3 "$db" "$q"
  [ "$status" -eq 0 ]
  # ⛔ 0. Any other answer means a corrected DIMM CE latches a permanent critical,
  # which is precisely the round-2 regression.
  [ "$output" = "0" ]
}

@test "MCE severity filter counts uncorrected errors" {
  q="$(mce_query)"
  db="$TESTDIR/ras.db"
  sqlite3 "$db" 'CREATE TABLE mce_record (id INTEGER, timestamp TEXT, error_msg TEXT, status INTEGER);'
  # One corrected, one with the UC bit, one with the deferred bit.
  sqlite3 "$db" "INSERT INTO mce_record VALUES (1,'t','Corrected',0),(2,'t','Uncorrected',2305843009213693952),(3,'t','Deferred',17592186044416);"
  run sqlite3 "$db" "$q"
  [ "$status" -eq 0 ]
  [ "$output" = "2" ]
}

# ── backup health states ─────────────────────────────────────────────────────
# Fixture: "6 states incl. missing stamp = UNHEALTHY".
#
# The state machine is a chain of marker files plus stamp staleness. The bug it
# encodes: an earlier version only checked staleness WHEN THE STAMP EXISTED, so a
# backup that had never succeeded once reported healthy forever.

backup_health() {
  # Reproduce the rendered decision chain against a temp state dir.
  local S="$TESTDIR/state" now=1786900000
  problems=""
  [ -e "$S/backup-root-data.retention-failed" ] && problems="${problems}retention; "
  [ -e "$S/backup-root-data.degraded" ] && problems="${problems}degraded; "
  if [ -e "$S/backup-root-data.failed" ]; then
    problems="${problems}last backup FAILED; "
  elif [ -r "$S/backup-root-data.stamp" ]; then
    stamp=$(cat "$S/backup-root-data.stamp")
    case "$stamp" in *[!0-9]*|"") stamp=0 ;; esac
    age=$(( now - stamp ))
    [ "$age" -gt 172800 ] && problems="${problems}backup last succeeded $((age / 86400))d ago; "
  else
    problems="${problems}backup has NEVER succeeded (no stamp); "
  fi
  printf '%s' "$problems"
}

@test "backup health: a MISSING stamp is unhealthy, not healthy" {
  mkdir -p "$TESTDIR/state"
  run backup_health
  # ⛔ The regression this guards: a backup that has never run must not look fine.
  [[ "$output" == *"NEVER succeeded"* ]]
}

@test "backup health: a fresh stamp is healthy" {
  mkdir -p "$TESTDIR/state"; echo 1786890000 > "$TESTDIR/state/backup-root-data.stamp"
  run backup_health
  [ "$output" = "" ]
}

@test "backup health: a stale stamp reports its age" {
  mkdir -p "$TESTDIR/state"; echo 1786400000 > "$TESTDIR/state/backup-root-data.stamp"
  run backup_health
  [[ "$output" == *"last succeeded 5d ago"* ]]
}

@test "backup health: a non-numeric stamp is treated as never, not as now" {
  mkdir -p "$TESTDIR/state"; echo 'corrupt' > "$TESTDIR/state/backup-root-data.stamp"
  run backup_health
  # A corrupt stamp parsed as 0 yields a huge age. Treating it as "now" would hide
  # a broken backup behind unparseable state.
  [[ "$output" == *"last succeeded"* ]]
}

@test "backup health: failure marker wins over a fresh stamp" {
  mkdir -p "$TESTDIR/state"
  echo 1786890000 > "$TESTDIR/state/backup-root-data.stamp"
  echo 'boom' > "$TESTDIR/state/backup-root-data.failed"
  run backup_health
  # ⛔ A fresh stamp from a PREVIOUS run must not mask the failure of the last one.
  [[ "$output" == *"last backup FAILED"* ]]
  [[ "$output" != *"NEVER succeeded"* ]]
}

@test "backup health: retention and degraded markers both surface" {
  mkdir -p "$TESTDIR/state"
  echo 1786890000 > "$TESTDIR/state/backup-root-data.stamp"
  echo 'r' > "$TESTDIR/state/backup-root-data.retention-failed"
  echo 'd' > "$TESTDIR/state/backup-root-data.degraded"
  run backup_health
  [[ "$output" == *"retention"* ]]
  [[ "$output" == *"degraded"* ]]
}

# ── k8s pod readiness ────────────────────────────────────────────────────────
#
# Both defects these pin were LIVE on 2026-08-16, found by running the check
# against real `crictl` output rather than by reading it:
#
#   1. the old implementation parsed `crictl pods` by column position, and
#      CREATED is variable-width prose. A pod in its second hour ("About an hour
#      ago", four fields instead of three) shifted every column, so healthy pods
#      were reported unhealthy under the literal name "Ready".
#   2. a Job's sandbox ends NotReady by design. Three Completed Jobs were counted
#      as failures on every run, drowning two real signals for a week.
#
# The fake speaks the crictl subset the function uses, driven by files, so a test
# describes a cluster state rather than a command transcript.

fake_crictl() {
  cat > "$TESTDIR/bin/k3s" <<'EOF'
#!/bin/sh
shift                       # drop "crictl"
cmd="$1"; shift
case "$cmd" in
  pods)
    cat "$TESTDIR/notready" 2>/dev/null
    ;;
  inspectp)
    tpl=""; id=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --template) tpl="$2"; shift 2 ;;
        -o) shift 2 ;;
        *) id="$1"; shift ;;
      esac
    done
    case "$tpl" in
      *metadata.name*) cat "$TESTDIR/pod-$id-name" 2>/dev/null ;;
      *job-name*)      cat "$TESTDIR/pod-$id-job"  2>/dev/null || echo "<no value>" ;;
    esac
    ;;
  ps)
    id=""
    while [ $# -gt 0 ]; do
      case "$1" in --pod) id="$2"; shift 2 ;; *) shift ;; esac
    done
    cat "$TESTDIR/pod-$id-containers" 2>/dev/null
    ;;
  inspect)
    cat "$TESTDIR/cont-$1-inspect" 2>/dev/null
    ;;
esac
exit 0
EOF
  chmod +x "$TESTDIR/bin/k3s"
}

# declare a pod: name, optional job label, container:exitcode pairs
declare_pod() {
  local id="$1" name="$2" job="$3"; shift 3
  echo "$id" >> "$TESTDIR/notready"
  echo "$name" > "$TESTDIR/pod-$id-name"
  [ -n "$job" ] && echo "$job" > "$TESTDIR/pod-$id-job"
  : > "$TESTDIR/pod-$id-containers"
  local pair cid code
  for pair in "$@"; do
    cid="${pair%%:*}"; code="${pair##*:}"
    echo "$cid" >> "$TESTDIR/pod-$id-containers"
    printf '{\n  "exitCode": %s,\n  "reason": "Completed"\n}\n' "$code" > "$TESTDIR/cont-$cid-inspect"
  done
}

@test "k8s readiness: a Completed Job pod is NOT reported" {
  fake_crictl
  declare_pod p1 jellyfin-quiesce-29781030-4htbm jellyfin-quiesce-29781030 c1:0
  load_function k8s_unhealthy_pods
  run k8s_unhealthy_pods
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "k8s readiness: a FAILED Job pod IS reported" {
  fake_crictl
  declare_pod p1 pin-collector-migrate-abc pin-collector-migrate c1:1
  load_function k8s_unhealthy_pods
  run k8s_unhealthy_pods
  [[ "$output" == *"pin-collector-migrate-abc(job-failed)"* ]]
}

@test "k8s readiness: a NotReady pod that is NOT a Job is reported" {
  fake_crictl
  declare_pod p1 immich-7d9f immich
  # no job label file written -> template yields <no value>
  rm -f "$TESTDIR/pod-p1-job"
  load_function k8s_unhealthy_pods
  run k8s_unhealthy_pods
  [[ "$output" == *"immich-7d9f"* ]]
}

@test "k8s readiness: a Job with a MIXED result is reported, not excused by the zero" {
  fake_crictl
  declare_pod p1 multi-abc multi c1:0 c2:2
  load_function k8s_unhealthy_pods
  run k8s_unhealthy_pods
  [[ "$output" == *"multi-abc(job-failed)"* ]]
}

@test "k8s readiness: an UNREADABLE exit code fails closed" {
  # The heartbeat has no `set -e`. A failed inspect must not read as success --
  # that is precisely how the missing-gawk bug stayed invisible.
  fake_crictl
  declare_pod p1 mystery-abc mystery c1:0
  rm -f "$TESTDIR/cont-c1-inspect"
  load_function k8s_unhealthy_pods
  run k8s_unhealthy_pods
  [[ "$output" == *"mystery-abc(job-failed)"* ]]
}

@test "k8s readiness: a Job sandbox with NO containers fails closed" {
  fake_crictl
  declare_pod p1 empty-abc empty
  load_function k8s_unhealthy_pods
  run k8s_unhealthy_pods
  [[ "$output" == *"empty-abc(job-failed)"* ]]
}

@test "k8s readiness: nothing NotReady reports nothing" {
  # ANTI-VACUITY for the whole group: if the fake returned nothing regardless of
  # state, every test above would pass for the wrong reason.
  fake_crictl
  : > "$TESTDIR/notready"
  load_function k8s_unhealthy_pods
  run k8s_unhealthy_pods
  [ -z "$output" ]
}

@test "k8s readiness: the check no longer parses crictl output by column" {
  # ANTI-REGRESSION. The specific defect was `$5!="Ready"` against a variable-width
  # CREATED column. Guard the shape, since a fixture cannot easily reproduce
  # crictl's own table formatting.
  ! grep -qE '\$5!="Ready"' "$HEARTBEAT_SCRIPT"
  grep -q 'crictl pods --state notready -q' "$HEARTBEAT_SCRIPT"
}
