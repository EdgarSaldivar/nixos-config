#!/bin/sh
# Root-only replacement of the installed observer probe with a pinned revision.
# The previous probe is kept as a root-only backup for a manual rollback.

set -eu

probe_source=/tmp/terracompute-probe.py
probe_directory=/usr/local/libexec
probe="$probe_directory/terracompute-observe"
previous="$probe.previous"
expected_probe_sha256=REPLACE_WITH_PROBE_SHA256

fail() {
  printf '%s\n' "observer update: $*" >&2
  exit 1
}

case "$expected_probe_sha256" in
  *REPLACE_WITH_*) fail 'pinned probe digest is still a placeholder' ;;
esac
[ "${#expected_probe_sha256}" -eq 64 ] || fail 'pinned probe digest is malformed'
case "$expected_probe_sha256" in
  *[!0-9a-f]*) fail 'pinned probe digest is malformed' ;;
esac

[ "$(id -u)" -eq 0 ] || fail 'must run as root'
[ "$(cat /sys/class/dmi/id/board_name)" = ROME2D32GM-2T ] || fail 'board identity mismatch'
[ "$(tr -d ' \t\r\n' < /etc/hostname)" = terracompute ] || fail 'hostname mismatch'
[ -f "$probe_source" ] && [ ! -L "$probe_source" ] || fail 'probe source missing or not a regular file'
[ -f "$probe" ] && [ ! -L "$probe" ] || fail 'installed probe missing or not a regular file'
[ "$(stat -c '%U:%G %a %h' "$probe")" = 'root:root 755 1' ] || fail 'installed probe is not root:root 0755'
[ ! -e "$previous" ] && [ ! -L "$previous" ] || fail 'previous probe backup already exists; inspect it instead of overwriting it'

actual_probe_sha256=$(sha256sum "$probe_source" | awk '{print $1}')
[ "$actual_probe_sha256" = "$expected_probe_sha256" ] || fail 'probe digest mismatch'
current_probe_sha256=$(sha256sum "$probe" | awk '{print $1}')
if [ "$current_probe_sha256" = "$expected_probe_sha256" ]; then
  printf '%s\n' "observer update: installed probe already matches $expected_probe_sha256; nothing changed"
  exit 0
fi

# Stage in the destination directory so the final mv is an atomic rename, and
# verify the root-owned staged copy rather than the world-writable source path.
staged=$(mktemp "$probe_directory/.terracompute-observe.XXXXXX")
trap 'rm -f "$staged"' EXIT
trap 'exit 1' HUP INT TERM
install -o root -g root -m 0755 "$probe_source" "$staged"
[ "$(sha256sum "$staged" | awk '{print $1}')" = "$expected_probe_sha256" ] || fail 'staged probe digest mismatch'
[ "$(stat -c '%U:%G %a %h' "$staged")" = 'root:root 755 1' ] || fail 'staged probe ownership or mode mismatch'

install -o root -g root -m 0700 "$probe" "$previous"
[ "$(sha256sum "$previous" | awk '{print $1}')" = "$current_probe_sha256" ] || fail 'previous probe backup digest mismatch'
[ "$(stat -c '%U:%G %a %h' "$previous")" = 'root:root 700 1' ] || fail 'previous probe backup ownership or mode mismatch'

mv -f "$staged" "$probe"

[ "$(sha256sum "$probe" | awk '{print $1}')" = "$expected_probe_sha256" ] || fail 'installed probe digest mismatch after replacement'
[ "$(stat -c '%U:%G %a %h' "$probe")" = 'root:root 755 1' ] || fail 'installed probe ownership or mode mismatch after replacement'

printf '%s\n' "observer update complete: probe=$expected_probe_sha256 previous=$current_probe_sha256"
