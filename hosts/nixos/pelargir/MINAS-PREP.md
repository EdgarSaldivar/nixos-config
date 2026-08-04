# minas preparation, WireGuard relight, and first backup

Site A wakes tomorrow. Items marked **Friend** require physical/router access at
site A; **Edgar** items use pelargir or minas administration. Preshared keys are
delivered out-of-band and never pasted into this document.

## 1. Relight the site-A router peer

**Friend or Edgar on the site-A router:** replace the old server peer with:

- public key: `O/X7SFFNUkUBl3F6YGHIlpEM3YNfQiw/uVaLSX96jQM=`
- endpoint: `pelargir.saldivar.io:51820`
- persistent keepalive: `25`
- preshared key: `REPLACE-FROM-SOPS` (out-of-band)
- tunnel addresses/routes matching pelargir's `192.168.4.2/32` router peer

Do not advertise the site's whole /20 to pelargir: its home LAN overlaps it.
Only site A's `10.0.1.0/24` is routed through this peer.

**Edgar on pelargir:** replace the `site_a_router` placeholder public key in
`wireguard.nix`, rebuild, and validate a recent handshake plus both targets:

```bash
set -euo pipefail
sudo wg show wg0
ping -c 3 10.0.1.6
ping -c 3 10.0.1.88
```

`10.0.1.6` is minas; `10.0.1.88` is its BMC. Stop if only the router answers.

## 2. Create the minas-side restricted SFTP account

**Edgar on minas:** create `pelargir-backup` and `/backups/pelargir`. Prefer an
OpenSSH `internal-sftp` chroot whose root is owned by root and not writable by
the account; the writable repository is one level below. If chroot is not
compatible with minas' layout, use an `authorized_keys` entry restricted with
`restrict,command="internal-sftp -d /backups/pelargir"` and no other keys.

One workable chroot layout is:

```bash
set -euo pipefail
sudo useradd --create-home --shell /usr/sbin/nologin pelargir-backup
sudo install -d -o root -g root -m 0755 /backups
sudo install -d -o pelargir-backup -g pelargir-backup -m 0700 /backups/pelargir
sudo install -d -o root -g root -m 0755 /etc/ssh/authorized_keys
sudo install -o root -g root -m 0600 /dev/null \
  /etc/ssh/authorized_keys/pelargir-backup
```

Put only pelargir's SSH **host public key** in that file, with the restriction
above, and configure an sshd `Match User pelargir-backup` block. Reusing that
stable identity avoids inventing an undeclared secret while minas still grants
it SFTP-only authority.
Validate sshd configuration before reload:

```bash
set -euo pipefail
sudo sshd -t
sudo systemctl reload sshd
```

## 3. Initialize exactly once, then test restore

**Edgar on pelargir:** after the SSH probe reaches the intended directory, run
the generated restic wrapper. It supplies repository/password configuration
without printing either secret.

```bash
set -euo pipefail
# An uninitialized repository makes `restic snapshots` fail by design. Reach
# the restricted account with the same identity first, then initialize once.
sudo sftp -b /dev/null \
  -i /etc/ssh/ssh_host_ed25519_key \
  pelargir-backup@minas.saldivar.io
sudo restic-minas init
sudo systemctl start restic-backups-minas.service
sudo restic-minas snapshots
restore_dir="$(mktemp -d)"
sudo restic-minas restore latest --target "$restore_dir" \
  --include /var/lib/restic-staging/pelargir/home-assistant-config/configuration.yaml
sudo test -f "$restore_dir/var/lib/restic-staging/pelargir/home-assistant-config/configuration.yaml"
printf 'Test restore retained at %s; inspect it, then remove it manually.\n' "$restore_dir"
```

The backup condition treats stale WG or failed SSH as a clean skip. The backup
itself scales HA, Z2M, and Mosquitto down, stages all local-path PVC data, and
uses systemd post-stop cleanup to scale them back up even if restic fails.

## 4. Join minas as a k3s agent over wg0

**Edgar on minas:** install native WireGuard, provision its private key and the
`wireguard_psk_minas` value out-of-band, and assign `192.168.4.6/32`. Configure
its pelargir peer with endpoint `pelargir.saldivar.io:51820`, server public key
above, persistent keepalive 25, and tunnel routes for `192.168.4.0/24` plus the
cluster networks. On pelargir, replace the `minas_agent` public-key placeholder.

Write the k3s token to a root-only file on minas, then join exclusively over wg0:

```bash
set -euo pipefail
sudo install -o root -g root -m 0600 /dev/null /etc/rancher/k3s-token
sudo k3s agent \
  --server https://192.168.4.1:6443 \
  --token-file /etc/rancher/k3s-token \
  --node-ip 192.168.4.6 \
  --flannel-iface wg0
```

Run the agent as a systemd service after the foreground proof. **Edgar on
pelargir:** read minas' assigned pod CIDR, append that exact `/24` to the
`minas_agent.allowedIPs`, rebuild, then confirm pod-to-pod traffic. Do not guess
the CIDR before the first join.

```bash
set -euo pipefail
sudo k3s kubectl get node minas -o jsonpath='{.spec.podCIDR}{"\n"}'
sudo wg show wg0
```

Future osgiliath/nardol nodes follow the same wg0 join rule even if physically
LAN-local; one overlay removes the site's overlapping-subnet ambiguity.
