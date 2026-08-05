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

> **Do section 4 before this one.** Everything below addresses minas as
> `minas-tirith`, a MagicDNS name that does not resolve until minas has joined
> the tailnet. The old public name is not a fallback: `minas.saldivar.io`
> resolves to site A's inner-NAT private address, which is precisely why the
> `/etc/hosts` pin was deleted. Join minas, confirm it shows Online, then
> return here. (Ordering corrected 2026-08-04.)

**Edgar on pelargir:** after the SSH probe reaches the intended directory, run
the generated restic wrapper. It supplies repository/password configuration
without printing either secret.

```bash
set -euo pipefail
# Pin minas' host key on first contact. The backup preflight and restic's own
# sftp both run BatchMode=yes, which fails closed on an unknown host — and an
# unpinned first connection is also the one moment this transport could be
# redirected without notice.
sudo ssh -o StrictHostKeyChecking=accept-new \
  -i /etc/ssh/ssh_host_ed25519_key \
  pelargir-backup@minas-tirith true || true
# An uninitialized repository makes `restic snapshots` fail by design. Reach
# the restricted account with the same identity first, then initialize once.
sudo sftp -b /dev/null \
  -i /etc/ssh/ssh_host_ed25519_key \
  pelargir-backup@minas-tirith
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

## 4. Join minas as a k3s agent over Tailscale

Minas' agent configuration is now declarative in
`hosts/nixos/minas-tirith/k3s.nix`, with its sops declarations and runtime-only
vpn-auth template in `secrets.nix`. The controller-provisioned
`tailscale_auth_key` and `k3s_agent_token` already exist in
`secrets/minas-tirith.yaml`; do not print either value or put it in the Nix
store. The agent uses the less-privileged join credential, and the shared fleet
module keeps k3s and Flannel traffic scoped to `tailscale0`.

K3s vpn-auth performs minas' Tailscale login from its own rendered template;
do not run a competing `tailscale up`. **Edgar on minas:** apply the host
configuration, then confirm `minas-tirith`
appears online with `tag:fleet`, that MagicDNS resolves both short names, and
that the agent joined through the agent token:

```bash
set -euo pipefail
sudo tailscale status
getent hosts pelargir
getent hosts minas-tirith
sudo k3s kubectl get node minas-tirith -o wide
```

From pelargir, repeat `sudo k3s kubectl get nodes -o wide` and the cross-node
pod test in `INSTALL-RUNBOOK.md`. The official K3s Tailscale example still
shows an explicit Node ExternalIP while current K3s source discovers and sets
the VPN node IP; the runbook's dated deviation note records the discrepancy and
makes post-join validation mandatory.
Future osgiliath/nardol nodes follow this same tailnet join pattern.
