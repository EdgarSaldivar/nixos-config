#!/usr/bin/env bats
#
# Characterization tests for the minas backup program.
#
# ⛔ These test the RENDERED program, extracted from the evaluated NixOS config,
# not a copy. That is deliberate: a test against a copy drifts silently from what
# is deployed, and this program has no other test surface at all.
#
# ROADMAP called for these fixtures and recorded that each was verified by hand
# during the 2026-07-30 audit rounds and then thrown away. This is them, made
# permanent. They are written BEFORE the program is extracted into a
# writeShellApplication, so they pin current behaviour rather than describing
# whatever a refactor happens to produce.
#
# $BACKUP_SCRIPT is the path to the rendered script; the wrapper supplies it.

setup() {
  export TESTDIR="$BATS_TEST_TMPDIR"
  mkdir -p "$TESTDIR/bin"
  export PATH="$TESTDIR/bin:$PATH"
}

# Pull one shell function out of the rendered program and define it here, with
# store paths rewritten to whatever fake we put on PATH.
load_function() {
  local name="$1"
  local body
  body="$(awk -v fn="$name" '
    $0 ~ "^" fn "\\(\\) \\{" { inside = 1 }
    inside { print }
    inside && /^\}/ { exit }
  ' "$BACKUP_SCRIPT")"
  [ -n "$body" ] || { echo "could not find $name() in $BACKUP_SCRIPT" >&2; return 1; }
  # Rewrite absolute store paths to bare command names so fakes take effect.
  body="$(printf '%s\n' "$body" | sed -E 's#/nix/store/[a-z0-9]+-[^/]*/bin/##g')"
  eval "$body"
}

# ── snapshot rotation ────────────────────────────────────────────────────────
# ROADMAP fixture: "20 dailies keep=14 prunes exactly the 6 oldest".

fake_zfs_with() {
  cat > "$TESTDIR/bin/zfs" <<EOF
#!/usr/bin/env bash
if [ "\$1" = "list" ]; then cat "$TESTDIR/snapshots"; exit 0; fi
if [ "\$1" = "destroy" ]; then echo "\$2" >> "$TESTDIR/destroyed"; exit ${1:-0}; fi
exit 0
EOF
  chmod +x "$TESTDIR/bin/zfs"
}

@test "prune keeps 14 dailies and destroys exactly the 6 oldest of 20" {
  for i in $(seq -w 1 20); do echo "storage2/backup@daily-2026080$i"; done > "$TESTDIR/snapshots"
  fake_zfs_with 0
  load_function prune
  run prune daily 14
  [ "$status" -eq 0 ]
  [ -f "$TESTDIR/destroyed" ]
  [ "$(wc -l < "$TESTDIR/destroyed" | tr -d ' ')" -eq 6 ]
  # oldest first: creation-sorted input means the first six lines are the victims
  head -1 "$TESTDIR/destroyed" | grep -q 'daily-202608001'
  # and the 15th (first kept) must NOT be destroyed
  ! grep -q 'daily-202608007' "$TESTDIR/destroyed"
}

@test "prune destroys nothing when the count is at or below keep" {
  for i in $(seq -w 1 14); do echo "storage2/backup@daily-2026080$i"; done > "$TESTDIR/snapshots"
  fake_zfs_with 0
  load_function prune
  run prune daily 14
  [ "$status" -eq 0 ]
  [ ! -f "$TESTDIR/destroyed" ]
}

@test "prune ignores snapshots of a different prefix" {
  { for i in $(seq -w 1 20); do echo "storage2/backup@weekly-2026080$i"; done; } > "$TESTDIR/snapshots"
  fake_zfs_with 0
  load_function prune
  run prune daily 14
  [ "$status" -eq 0 ]
  [ ! -f "$TESTDIR/destroyed" ]
}

@test "prune returns non-zero when a destroy fails, and keeps going" {
  for i in $(seq -w 1 20); do echo "storage2/backup@daily-2026080$i"; done > "$TESTDIR/snapshots"
  cat > "$TESTDIR/bin/zfs" <<EOF
#!/usr/bin/env bash
if [ "\$1" = "list" ]; then cat "$TESTDIR/snapshots"; exit 0; fi
if [ "\$1" = "destroy" ]; then echo "\$2" >> "$TESTDIR/destroyed"; exit 1; fi
exit 0
EOF
  chmod +x "$TESTDIR/bin/zfs"
  load_function prune
  run prune daily 14
  # ⛔ THE POINT: a failure on an early snapshot must not abandon the rest, and
  # the non-zero return is what the heartbeat sees. Silent accumulation until the
  # pool fills is the failure this guards.
  [ "$status" -ne 0 ]
  [ "$(wc -l < "$TESTDIR/destroyed" | tr -d ' ')" -eq 6 ]
}
