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
# ROADMAP fixture: "must count 2 on a mixed log, not 6".
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
# ROADMAP fixture: "RAW_VALUE column, ATA and NVMe forms; 24 -> 27 must alert".

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
