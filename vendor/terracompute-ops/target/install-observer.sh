#!/bin/sh
# One-time, root-only installation of the forced-command target observer.

set -eu

observer=terracompute-observer
probe_source=/tmp/terracompute-probe.py
public_key_source=/tmp/palantir-observer.pub
probe=/usr/local/libexec/terracompute-observe
state=/var/lib/terracompute-observer
home=/var/empty/terracompute-observer
authorized_keys="$home/.ssh/authorized_keys"
sudoers=/etc/sudoers.d/terracompute-observer
# Filled in by the operator from the probe being installed, as update-observer.sh is.
# A concrete pin here goes stale the moment the probe changes and then refuses the
# very probe shipped beside it while looking correct.
expected_probe_sha256=REPLACE_WITH_PROBE_SHA256
expected_key_sha256='SHA256:yd6PfVsZ/BdQ7LAZY8GHcS2swS1Y3aKEdI2EwijbM0U'

fail() {
  printf '%s\n' "observer install: $*" >&2
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
[ -f "$probe_source" ] || fail 'probe source missing'
[ -f "$public_key_source" ] || fail 'public key source missing'

actual_probe_sha256=$(sha256sum "$probe_source" | awk '{print $1}')
[ "$actual_probe_sha256" = "$expected_probe_sha256" ] || fail 'probe digest mismatch'
actual_key_sha256=$(ssh-keygen -lf "$public_key_source" -E sha256 | awk '{print $2}')
[ "$actual_key_sha256" = "$expected_key_sha256" ] || fail 'observer public-key fingerprint mismatch'

python=$(readlink -f /usr/bin/python3)
[ -x "$python" ] || fail 'system Python is unavailable'
[ "$(stat -c '%U:%G' "$python")" = root:root ] || fail 'system Python is not root-owned'

filesystem=$(findmnt -n -o FSTYPE -T /var/lib)
case "$filesystem" in
  nfs|nfs4|cifs|fuse.*|tmpfs|ramfs|overlay)
    fail "unsupported state filesystem: $filesystem"
    ;;
esac

if getent passwd "$observer" >/dev/null; then
  fail 'observer account already exists; inspect it instead of overwriting it'
fi
for path in "$probe" "$state" "$home" "$sudoers"; do
  [ ! -e "$path" ] || fail "installation path already exists: $path"
done

useradd --system --home-dir "$home" --no-create-home --shell /bin/sh "$observer"
# No password hash can authenticate, while public-key authentication remains usable.
usermod --password '*' "$observer"

install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "$probe_source" "$probe"

install -d -o "$observer" -g "$observer" -m 0700 "$state"
install -o "$observer" -g "$observer" -m 0600 /dev/null "$state/probe.lock"
install -o "$observer" -g "$observer" -m 0600 /dev/null "$state/probe-state.json"
printf '%s\n' '{"phase":"idle","version":1}' > "$state/probe-state.json"
chown "$observer:$observer" "$state/probe-state.json"
chmod 0600 "$state/probe-state.json"

install -d -o root -g root -m 0755 "$home"
install -d -o root -g "$observer" -m 0750 "$home/.ssh"
public_key=$(cat "$public_key_source")
printf 'restrict,command="/usr/bin/sudo -n %s" %s\n' "$probe" "$public_key" > "$authorized_keys"
chown root:"$observer" "$authorized_keys"
chmod 0640 "$authorized_keys"

cat > "$sudoers" <<EOF
Defaults:$observer env_reset,secure_path=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
$observer ALL=(root) NOPASSWD: $probe
EOF
chown root:root "$sudoers"
chmod 0440 "$sudoers"

visudo -cf "$sudoers" >/dev/null
sshd -t

[ "$(id -Gn "$observer")" = "$observer" ] || fail 'observer acquired unexpected supplementary groups'
test "$(stat -c '%U:%G %a %h' "$probe")" = 'root:root 755 1'
test "$(stat -c '%U:%G %a %h' "$authorized_keys")" = "root:$observer 640 1"
test "$(stat -c '%U:%G %a %h' "$state/probe.lock")" = "$observer:$observer 600 1"
test "$(stat -c '%U:%G %a %h' "$state/probe-state.json")" = "$observer:$observer 600 1"

printf '%s\n' "observer install complete: probe=$actual_probe_sha256 key=$actual_key_sha256 filesystem=$filesystem"
