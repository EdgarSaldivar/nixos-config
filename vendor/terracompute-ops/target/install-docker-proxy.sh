#!/bin/sh
# Root-only install of the docker socket proxy and its unit.
#
# The proxy gives an observation a real docker socket that cannot touch a tenant's
# container. Nothing else on the machine is meant to use it: dockerd keeps its own
# socket, every existing client keeps talking to that, and only the observe profile
# has the proxy bound over /run/docker.sock inside its own mount namespace.
#
# Re-runnable. If the installed copy already matches the pinned digest it changes
# nothing and says so.

set -eu

proxy_source=/tmp/terracompute-docker-proxy.py
libexec=/usr/local/libexec
proxy="$libexec/terracompute-docker-proxy"
unit=/etc/systemd/system/terracompute-docker-proxy.service
expected_proxy_sha256=REPLACE_WITH_PROXY_SHA256

fail() {
  printf '%s\n' "docker proxy install: $*" >&2
  exit 1
}

case "$expected_proxy_sha256" in
  *REPLACE_WITH_*) fail 'pinned proxy digest is still a placeholder' ;;
esac
[ "${#expected_proxy_sha256}" -eq 64 ] || fail 'pinned proxy digest is malformed'
case "$expected_proxy_sha256" in
  *[!0-9a-f]*) fail 'pinned proxy digest is malformed' ;;
esac

[ "$(id -u)" -eq 0 ] || fail 'must run as root'
[ "$(cat /sys/class/dmi/id/board_name)" = ROME2D32GM-2T ] || fail 'board identity mismatch'
[ "$(tr -d ' \t\r\n' < /etc/hostname)" = terracompute ] || fail 'hostname mismatch'
[ -f "$proxy_source" ] && [ ! -L "$proxy_source" ] || fail 'proxy source missing or not a regular file'
[ -S /var/run/docker.sock ] || fail 'there is no docker socket to proxy'

actual=$(sha256sum "$proxy_source" | awk '{print $1}')
[ "$actual" = "$expected_proxy_sha256" ] || fail 'proxy digest mismatch'

# Stage in the destination directory so the final move is an atomic rename, and check
# the root-owned staged copy rather than the world-writable source path.
staged=$(mktemp "$libexec/.terracompute-docker-proxy.XXXXXX")
trap 'rm -f "$staged"' EXIT
trap 'exit 1' HUP INT TERM
install -o root -g root -m 0755 "$proxy_source" "$staged"
[ "$(sha256sum "$staged" | awk '{print $1}')" = "$expected_proxy_sha256" ] || fail 'staged proxy digest mismatch'
mv -f "$staged" "$proxy"
trap - EXIT

# RuntimeDirectory, not a bare path in /run: systemd removes the directory when the
# unit stops, so the socket goes with it. That matters -- the helper decides whether
# to hand an observation the proxy or a blanked socket by asking whether a socket is
# there, and a path left behind by a dead proxy would answer yes.
cat > "$unit" <<'UNIT'
[Unit]
Description=Docker socket that refuses a tenant's container
Documentation=https://github.com/terracompute/ops target/terracompute-docker-proxy.py
After=docker.service
Wants=docker.service

[Service]
Type=exec
ExecStart=/usr/local/libexec/terracompute-docker-proxy
RuntimeDirectory=terracompute-docker-proxy
RuntimeDirectoryMode=0700
Restart=always
RestartSec=2
# Root, because it holds the docker socket. Everything it does not need is removed.
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
# It speaks to dockerd over a unix socket and listens on one. It has no business
# opening a network connection, and this is the process holding the real socket.
RestrictAddressFamilies=AF_UNIX
MemoryMax=256M
TasksMax=256

[Install]
WantedBy=multi-user.target
UNIT
chmod 0644 "$unit"

systemctl daemon-reload
systemctl enable --now terracompute-docker-proxy.service
systemctl restart terracompute-docker-proxy.service

# Wait for the listener rather than declaring success on the unit being started:
# the helper's choice between the proxy and a blank turns on the socket existing.
i=0
while [ "$i" -lt 50 ]; do
  [ -S /run/terracompute-docker-proxy/docker.sock ] \
    && [ -S /run/terracompute-docker-proxy/docker-ro.sock ] && break
  i=$((i + 1))
  sleep 0.1
done
[ -S /run/terracompute-docker-proxy/docker.sock ] || fail 'the proxy started but is not listening'
# The read-only socket is what an observation binds. Without it the helper finds no
# socket there and blanks docker, which fails closed -- but silently, and the read
# loop simply loses docker again.
[ -S /run/terracompute-docker-proxy/docker-ro.sock ] || fail 'the read-only socket is missing'

printf '%s\n' "docker proxy install: $expected_proxy_sha256 listening, read-write and read-only"
