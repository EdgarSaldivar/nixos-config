#!/bin/sh
# Root-only replacement of the installed action helper with a pinned revision.
# The previous helper is kept as a root-only backup for a manual rollback. The
# account, key, sudoers rule and ledger are left exactly as they are.

set -eu

helper_source=/tmp/terracompute-act.py
helper_directory=/usr/local/libexec
helper="$helper_directory/terracompute-act"
previous="$helper.previous"
ledger=/var/lib/terracompute-actor/ledger
expected_helper_sha256=REPLACE_WITH_HELPER_SHA256

fail() {
  printf '%s\n' "actor update: $*" >&2
  exit 1
}

case "$expected_helper_sha256" in
  *REPLACE_WITH_*) fail 'pinned helper digest is still a placeholder' ;;
esac
[ "${#expected_helper_sha256}" -eq 64 ] || fail 'pinned helper digest is malformed'
case "$expected_helper_sha256" in
  *[!0-9a-f]*) fail 'pinned helper digest is malformed' ;;
esac

[ "$(id -u)" -eq 0 ] || fail 'must run as root'
[ "$(cat /sys/class/dmi/id/board_name)" = ROME2D32GM-2T ] || fail 'board identity mismatch'
[ "$(tr -d ' \t\r\n' < /etc/hostname)" = terracompute ] || fail 'hostname mismatch'
[ -f "$helper_source" ] && [ ! -L "$helper_source" ] || fail 'helper source missing or not a regular file'
[ -f "$helper" ] && [ ! -L "$helper" ] || fail 'installed helper missing or not a regular file'
[ "$(stat -c '%U:%G %a %h' "$helper")" = 'root:root 755 1' ] || fail 'installed helper is not root:root 0755'
[ ! -e "$previous" ] && [ ! -L "$previous" ] || fail 'previous helper backup already exists; inspect it instead of overwriting it'

actual_helper_sha256=$(sha256sum "$helper_source" | awk '{print $1}')
[ "$actual_helper_sha256" = "$expected_helper_sha256" ] || fail 'helper digest mismatch'
current_helper_sha256=$(sha256sum "$helper" | awk '{print $1}')
if [ "$current_helper_sha256" = "$expected_helper_sha256" ]; then
  printf '%s\n' "actor update: installed helper already matches $expected_helper_sha256; nothing changed"
  exit 0
fi

# A running helper holds an exclusive lock on the ledger directory from before its
# claim until it exits. Replacing the file underneath one is safe, but waiting means
# the next request meets the new helper rather than racing the old one's finish.
if [ -d "$ledger" ] && command -v flock >/dev/null 2>&1; then
  flock --exclusive --timeout 120 "$ledger" true || fail 'an execution is in progress; retry when it finishes'
fi

# Stage in the destination directory so the final mv is an atomic rename, and verify
# the root-owned staged copy rather than the world-writable source path.
staged=$(mktemp "$helper_directory/.terracompute-act.XXXXXX")
trap 'rm -f "$staged"' EXIT
trap 'exit 1' HUP INT TERM
install -o root -g root -m 0755 "$helper_source" "$staged"
[ "$(sha256sum "$staged" | awk '{print $1}')" = "$expected_helper_sha256" ] || fail 'staged helper digest mismatch'
[ "$(stat -c '%U:%G %a %h' "$staged")" = 'root:root 755 1' ] || fail 'staged helper ownership or mode mismatch'

install -o root -g root -m 0700 "$helper" "$previous"
[ "$(sha256sum "$previous" | awk '{print $1}')" = "$current_helper_sha256" ] || fail 'previous helper backup digest mismatch'
[ "$(stat -c '%U:%G %a %h' "$previous")" = 'root:root 700 1' ] || fail 'previous helper backup ownership or mode mismatch'

mv -f "$staged" "$helper"

[ "$(sha256sum "$helper" | awk '{print $1}')" = "$expected_helper_sha256" ] || fail 'installed helper digest mismatch after replacement'
[ "$(stat -c '%U:%G %a %h' "$helper")" = 'root:root 755 1' ] || fail 'installed helper ownership or mode mismatch after replacement'
# The sudoers rule names this path, so nothing else needs to change.
[ -f /etc/sudoers.d/terracompute-actor ] || fail 'actor sudoers rule is missing'
grep -q "$helper" /etc/sudoers.d/terracompute-actor || fail 'actor sudoers rule no longer names this helper'

printf '%s\n' "actor update complete: helper=$expected_helper_sha256 previous=$current_helper_sha256"
