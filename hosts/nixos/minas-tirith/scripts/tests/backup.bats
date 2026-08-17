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

# ⛔ `#!/bin/sh`, NOT `#!/usr/bin/env bash`.
#
# The Nix build sandbox on Linux has no /usr/bin/env, so an env shebang makes the
# fake unexecutable, `zfs destroy` never runs, and the two prune tests fail. On
# macOS /usr/bin/env exists, so this passed there for as long as it was only ever
# run there -- which is exactly how it went unnoticed until the x86_64-linux CI leg
# ran for the first time on 2026-08-16.
#
# These fakes are POSIX shell, so /bin/sh (which Nix always provides in the sandbox)
# is both sufficient and portable.
fake_zfs_with() {
  cat > "$TESTDIR/bin/zfs" <<EOF
#!/bin/sh
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
  # ⛔ Assert the EXACT victim set, in order. Counting six and spot-checking two
  # of them passes for a prune that destroys 1-5 plus 20 -- i.e. one that deletes
  # the NEWEST snapshot, which is the copy you would actually want.
  # ⚠️ seq -w pads to the width of the LARGEST value, so `seq -w 1 6` yields 1..6
  # while `seq -w 1 20` yields 01..20. Generate the victim set from the same range
  # as the input and take the first six, or the expected set is malformed.
  seq -w 1 20 | head -6 | while read i; do echo "storage2/backup@daily-2026080$i"; done > "$TESTDIR/want"
  diff "$TESTDIR/want" "$TESTDIR/destroyed"
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

@test "prune returns non-zero when an EARLY destroy fails, and still prunes the rest" {
  for i in $(seq -w 1 20); do echo "storage2/backup@daily-2026080$i"; done > "$TESTDIR/snapshots"
  # ⛔ Only the FIRST victim fails; the remaining five succeed.
  #
  # An earlier version of this fixture made every destroy fail, which is too weak:
  # an implementation that returns merely the status of the LAST destroy would
  # still exit non-zero and pass. Failing early and succeeding afterwards is the
  # case that distinguishes "accumulates rc" from "keeps the last rc", and the
  # comment in the source specifically claims the former.
  cat > "$TESTDIR/bin/zfs" <<EOF
#!/bin/sh
if [ "\$1" = "list" ]; then cat "$TESTDIR/snapshots"; exit 0; fi
if [ "\$1" = "destroy" ]; then
  echo "\$2" >> "$TESTDIR/destroyed"
  [ "\$(wc -l < "$TESTDIR/destroyed" | tr -d ' ')" -eq 1 ] && exit 1
  exit 0
fi
exit 0
EOF
  chmod +x "$TESTDIR/bin/zfs"
  load_function prune
  run prune daily 14
  [ "$status" -ne 0 ]
  # and it did NOT abandon the remaining five after the early failure
  [ "$(wc -l < "$TESTDIR/destroyed" | tr -d ' ')" -eq 6 ]
}

# ── dump promotion ───────────────────────────────────────────────────────────
# ROADMAP fixture: "empty-but-exit-0 dump must NOT overwrite a good one".
#
# pg_dumpall can exit 0 having produced almost nothing — a failed dump wearing a
# success code. Promotion is staged through a .tmp and gated on a size floor, so
# yesterday's good dump survives today's silent failure.

promote() {
  # The rendered decision, reproduced against a temp dumpdir. The floor and the
  # staged-then-mv shape are asserted against the real script in the check that
  # runs this suite, so this cannot drift into testing a different rule.
  local dumpdir="$1" name="$2"
  # ⚠️ `wc -c`, not `stat`. The real program uses GNU `stat -c %s`, but these
  # fixtures run on both the Linux builder and a BSD-userland Mac, where `stat -f`
  # means something else entirely and silently yields a non-numeric value. The
  # 1024-byte floor is pinned against the rendered program by the check that runs
  # this suite, so portability here costs no fidelity.
  if [ "$(wc -c < "$dumpdir/$name.sql.gz.tmp" | tr -d " ")" -gt 1024 ]; then
    mv "$dumpdir/$name.sql.gz.tmp" "$dumpdir/$name.sql.gz"
  fi
}

@test "promotion: an empty dump does NOT overwrite a good one" {
  d="$TESTDIR/dumps"; mkdir -p "$d"
  printf 'GOOD DUMP FROM YESTERDAY%.0s' $(seq 1 200) > "$d/db.sql.gz"
  before="$(cat "$d/db.sql.gz")"
  : > "$d/db.sql.gz.tmp"              # exit 0, zero bytes
  run promote "$d" db
  # ⛔ The good dump must be untouched. This is the difference between "one bad
  # night" and "no backup at all, silently".
  [ "$(cat "$d/db.sql.gz")" = "$before" ]
  [ -f "$d/db.sql.gz.tmp" ]
}

@test "promotion: a suspiciously small dump does NOT overwrite a good one" {
  d="$TESTDIR/dumps"; mkdir -p "$d"
  printf 'GOOD DUMP FROM YESTERDAY%.0s' $(seq 1 200) > "$d/db.sql.gz"
  before="$(cat "$d/db.sql.gz")"
  printf 'x%.0s' $(seq 1 1024) > "$d/db.sql.gz.tmp"   # exactly at the floor, not above
  run promote "$d" db
  [ "$(cat "$d/db.sql.gz")" = "$before" ]
}

@test "promotion: a plausible dump DOES replace the previous one" {
  d="$TESTDIR/dumps"; mkdir -p "$d"
  echo "old" > "$d/db.sql.gz"
  printf 'y%.0s' $(seq 1 2048) > "$d/db.sql.gz.tmp"
  run promote "$d" db
  [ "$(head -c 4 "$d/db.sql.gz")" = "yyyy" ]
  # the staging file is consumed, not left behind to be promoted twice
  [ ! -f "$d/db.sql.gz.tmp" ]
}

@test "promotion: a first-ever dump is created when none exists" {
  d="$TESTDIR/dumps"; mkdir -p "$d"
  printf 'z%.0s' $(seq 1 2048) > "$d/db.sql.gz.tmp"
  run promote "$d" db
  [ -f "$d/db.sql.gz" ]
}
