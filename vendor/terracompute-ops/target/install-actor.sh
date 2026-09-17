#!/bin/sh
# One-time, root-only installation of the forced-command target action helper.
# The pinned helper digest and key fingerprint are filled in at review time; the
# script refuses to run while either placeholder remains.

set -eu

actor=terracompute-actor
helper_source=/tmp/terracompute-act.py
public_key_source=/tmp/palantir-actor.pub
helper=/usr/local/libexec/terracompute-act
state=/var/lib/terracompute-actor
ledger="$state/ledger"
home=/var/empty/terracompute-actor
authorized_keys="$home/.ssh/authorized_keys"
sudoers=/etc/sudoers.d/terracompute-actor
secure_path=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
expected_helper_sha256=REPLACE_WITH_HELPER_SHA256
expected_key_sha256=REPLACE_WITH_ACTOR_KEY_SHA256

fail() {
  printf '%s\n' "actor install: $*" >&2
  exit 1
}

case "$expected_helper_sha256 $expected_key_sha256" in
  *REPLACE_WITH_*) fail 'pinned helper digest or key fingerprint is still a placeholder' ;;
esac
[ "${#expected_helper_sha256}" -eq 64 ] || fail 'pinned helper digest is malformed'
case "$expected_helper_sha256" in
  *[!0-9a-f]*) fail 'pinned helper digest is malformed' ;;
esac
case "$expected_key_sha256" in
  SHA256:*) ;;
  *) fail 'pinned key fingerprint is malformed' ;;
esac
expected_key_hash=${expected_key_sha256#SHA256:}
[ "${#expected_key_hash}" -eq 43 ] || fail 'pinned key fingerprint is malformed'
case "$expected_key_hash" in
  *[!A-Za-z0-9+/]*) fail 'pinned key fingerprint is malformed' ;;
esac

[ "$(id -u)" -eq 0 ] || fail 'must run as root'
[ "$(cat /sys/class/dmi/id/board_name)" = ROME2D32GM-2T ] || fail 'board identity mismatch'
[ "$(tr -d ' \t\r\n' < /etc/hostname)" = terracompute ] || fail 'hostname mismatch'
[ -f "$helper_source" ] && [ ! -L "$helper_source" ] || fail 'helper source missing or not a regular file'
[ -f "$public_key_source" ] && [ ! -L "$public_key_source" ] || fail 'public key source missing or not a regular file'

actual_helper_sha256=$(sha256sum "$helper_source" | awk '{print $1}')
[ "$actual_helper_sha256" = "$expected_helper_sha256" ] || fail 'helper digest mismatch'

# Read the key once, accept only a bare ssh-ed25519 key, and fingerprint exactly
# the normalized line that is written, so key options cannot be smuggled in.
public_key_line=$(cat "$public_key_source")
newline='
'
case "$public_key_line" in
  *"$newline"*) fail 'public key source must be a single line' ;;
esac
key_type=${public_key_line%% *}
key_rest=${public_key_line#* }
key_blob=${key_rest%% *}
[ "$key_type" = ssh-ed25519 ] || fail 'public key must be a bare ssh-ed25519 key'
case "$key_blob" in
  ''|*[!A-Za-z0-9+/=]*) fail 'public key blob is malformed' ;;
esac
public_key="$key_type $key_blob"
actual_key_sha256=$(printf '%s\n' "$public_key" | ssh-keygen -lf - -E sha256 | awk '{print $2}')
[ "$actual_key_sha256" = "$expected_key_sha256" ] || fail 'actor public-key fingerprint mismatch'

python=$(readlink -f /usr/bin/python3)
[ -x "$python" ] || fail 'system Python is unavailable'
[ "$(stat -c '%U:%G' "$python")" = root:root ] || fail 'system Python is not root-owned'
/usr/bin/python3 -I -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' \
  || fail 'system Python is older than 3.10'
[ -x /usr/bin/docker ] || fail 'docker CLI is unavailable'
[ "$(stat -c '%U:%G' "$(readlink -f /usr/bin/docker)")" = root:root ] || fail 'docker CLI is not root-owned'
[ -x /usr/bin/sudo ] || fail 'sudo is unavailable'

filesystem=$(findmnt -n -o FSTYPE -T /var/lib)
case "$filesystem" in
  nfs|nfs4|cifs|fuse.*|tmpfs|ramfs|overlay)
    fail "unsupported ledger filesystem: $filesystem"
    ;;
esac

if getent passwd "$actor" >/dev/null; then
  fail 'actor account already exists; inspect it instead of overwriting it'
fi
if getent group "$actor" >/dev/null; then
  fail 'actor group already exists; inspect it instead of overwriting it'
fi
for path in "$helper" "$state" "$home" "$sudoers"; do
  [ ! -e "$path" ] && [ ! -L "$path" ] || fail "installation path already exists: $path"
done

useradd --system --user-group --home-dir "$home" --no-create-home --shell /bin/sh "$actor"
# No password hash can authenticate, while public-key authentication remains usable.
usermod --password '*' "$actor"

install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "$helper_source" "$helper"
# The sudoers rule does not exist yet, so a mismatching copy is never reachable.
if [ "$(sha256sum "$helper" | awk '{print $1}')" != "$expected_helper_sha256" ]; then
  rm -f "$helper"
  fail 'installed helper digest mismatch'
fi

install -d -o root -g root -m 0700 "$state"
install -d -o root -g root -m 0700 "$ledger"

install -d -o root -g root -m 0755 "$home"
install -d -o root -g "$actor" -m 0750 "$home/.ssh"
printf 'restrict,command="/usr/bin/sudo -n %s" %s\n' "$helper" "$public_key" > "$authorized_keys"
chown root:"$actor" "$authorized_keys"
chmod 0640 "$authorized_keys"

# Validate the rule under a name sudo's includedir ignores, then rename it into place,
# so an invalid file never becomes live sudo configuration.
sudoers_staged=$(mktemp /etc/sudoers.d/.terracompute-actor.XXXXXX)
trap 'rm -f "$sudoers_staged"' EXIT
trap 'exit 1' HUP INT TERM
cat > "$sudoers_staged" <<EOF
Defaults:$actor env_reset,env_keep="SSH_ORIGINAL_COMMAND",secure_path=$secure_path
$actor ALL=(root) NOPASSWD: $helper ""
EOF
chown root:root "$sudoers_staged"
chmod 0440 "$sudoers_staged"
visudo -cf "$sudoers_staged" >/dev/null || fail 'sudoers rule failed validation'
mv -f "$sudoers_staged" "$sudoers"

visudo -cf "$sudoers" >/dev/null || fail 'installed sudoers rule failed validation'
sshd -t || fail 'sshd configuration test failed'

[ "$(id -Gn "$actor")" = "$actor" ] || fail 'actor acquired unexpected supplementary groups'
[ "$(getent passwd "$actor" | cut -d: -f6)" = "$home" ] || fail 'actor home mismatch'
[ "$(getent shadow "$actor" | cut -d: -f2)" = '*' ] || fail 'actor password is not disabled'
[ ! -L "$state" ] && [ ! -L "$ledger" ] || fail 'ledger path is a symlink'
[ "$(stat -c '%U:%G %a %h' "$helper")" = 'root:root 755 1' ] || fail 'helper ownership or mode mismatch'
[ "$(stat -c '%U:%G %a' "$state")" = 'root:root 700' ] || fail 'state directory ownership or mode mismatch'
[ "$(stat -c '%U:%G %a' "$ledger")" = 'root:root 700' ] || fail 'ledger directory ownership or mode mismatch'
[ "$(stat -c '%U:%G %a' "$home")" = 'root:root 755' ] || fail 'home ownership or mode mismatch'
[ "$(stat -c '%U:%G %a' "$home/.ssh")" = "root:$actor 750" ] || fail 'ssh directory ownership or mode mismatch'
[ "$(stat -c '%U:%G %a %h' "$authorized_keys")" = "root:$actor 640 1" ] || fail 'authorized_keys ownership or mode mismatch'
[ "$(stat -c '%U:%G %a %h' "$sudoers")" = 'root:root 440 1' ] || fail 'sudoers ownership or mode mismatch'
[ "$(sha256sum "$helper" | awk '{print $1}')" = "$expected_helper_sha256" ] || fail 'helper digest changed'

printf '%s\n' "actor install complete: helper=$expected_helper_sha256 key=$actual_key_sha256 filesystem=$filesystem"
