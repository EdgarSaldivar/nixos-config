# pelargir bare-metal install runbook

This is deliberately a no-kexec install. Start on the live Raspberry Pi OS
rescue card at `pelargir@10.0.0.165`; its disk is disposable, but the only disko
target remains the serial-qualified Kingston NVMe. Never put the rescue card
back in the slot after the cold-boot test.

## 1. EEPROM and physical prerequisites

On the Pi, update EEPROM first, reboot back into Raspberry Pi OS, and record the
reported bootloader version in the maintenance log. Open the Argon V5 case and
confirm that its RTC battery is actually fitted; k3s certificate validity is
clock-sensitive even though the unit waits for time sync.

```bash
set -euo pipefail
sudo rpi-eeprom-update -a
sudo reboot
```

After reconnecting:

```bash
set -euo pipefail
sudo rpi-eeprom-update
vcgencmd bootloader_version
```

## 2. Install Nix in the rescue OS

Use Determinate's multi-user installer, then start a fresh login shell.

```bash
set -euo pipefail
curl --proto '=https' --tlsv1.2 -sSfL https://install.determinate.systems/nix \
  | sh -s -- install --no-confirm
# This intentionally replaces the rescue shell after the installer changes its
# environment; there are no later commands in this copy-paste block.
# shellcheck disable=SC2093
exec "${SHELL}" -l
```

## 3. Trust the binary caches

nixos-raspberrypi 67616c2 declares its prebuilt vendor-kernel cache in both its
flake and `trusted-nix-caches` module. Add that cache alongside nix-community,
matching this repository's `nixConfig`, before evaluating the flake; a fresh
installer does not silently trust flake-level Nix configuration. The upstream
URL and key below were verified at the pinned rev on 2026-08-03.

```bash
set -euo pipefail
cache_line='extra-substituters = https://nix-community.cachix.org https://nixos-raspberrypi.cachix.org'
key_line='extra-trusted-public-keys = nix-community.cachix.org-1:mB9FSh9qf2dCimDSUo8Zy7bkq5CX+/rkCWyvRCYg3Fs= nixos-raspberrypi.cachix.org-1:4iMO9LXa8BqhU+Rpg6LQKiGa2lsNh/j2oiYLNOQ5sPI='
grep -qxF "$cache_line" /etc/nix/nix.conf \
  || printf '%s\n' "$cache_line" | sudo tee -a /etc/nix/nix.conf
grep -qxF "$key_line" /etc/nix/nix.conf \
  || printf '%s\n' "$key_line" | sudo tee -a /etc/nix/nix.conf
sudo systemctl restart nix-daemon.service
nix config show substituters | grep -F 'https://nix-community.cachix.org'
nix config show substituters | grep -F 'https://nixos-raspberrypi.cachix.org'
```

## 4. Partition only the pinned NVMe

Clone this repository on the Pi and verify that the by-id symlink names the
Kingston drive before allowing disko to destroy the leftover bare ext4 layout.

```bash
set -euo pipefail
git clone REPLACE-WITH-REPOSITORY-URL nixos-config
cd nixos-config
readlink -f /dev/disk/by-id/nvme-KINGSTON_SNVS1000G_50026B7685D2B59A
sudo nix --accept-flake-config run github:nix-community/disko -- \
  --mode disko --flake .#pelargir
findmnt /mnt
findmnt /mnt/boot/firmware
```

## 5. Install natively

Build the aarch64 closure on the Pi. nixos-raspberrypi 67616c2 selects its
matched `linuxPackages_rpi5` vendor kernel/firmware bundle, whose default is
6.18.39; the cache configured above should substitute that kernel. There is no
cross-build configuration in this host.

```bash
set -euo pipefail
sudo nixos-install --root /mnt --flake .#pelargir --no-root-passwd
```

## 6. Place the pre-generated host identity before first boot

From the Mac, copy the key pair into a temporary directory on the rescue OS:

```bash
set -euo pipefail
scp ~/Development/secrets/pelargir/ssh_host_ed25519_key \
  ~/Development/secrets/pelargir/ssh_host_ed25519_key.pub \
  pelargir@10.0.0.165:/tmp/
```

Then on the Pi, place it under the installed root with exact ownership and
modes. Sops decryption depends on this identity on the first activation.

```bash
set -euo pipefail
sudo install -o root -g root -m 0600 \
  /tmp/ssh_host_ed25519_key /mnt/etc/ssh/ssh_host_ed25519_key
sudo install -o root -g root -m 0644 \
  /tmp/ssh_host_ed25519_key.pub /mnt/etc/ssh/ssh_host_ed25519_key.pub
```

Re-run installation activation after adding the key so the sops templates are
rendered into the target before reboot:

```bash
set -euo pipefail
sudo nixos-install --root /mnt --flake .#pelargir --no-root-passwd
```

## 7. Populate and inspect direct-boot content

Verified at nixos-raspberrypi 67616c2: `bootloader = "kernel"` assigns a
generational builder to `system.build.installBootLoader`. `nixos-install` runs
that bootloader installer and later generation switches run it again; there is
no activation unit or one-shot migrator to invoke. The builder atomically
refreshes top-level firmware/config and `/boot/firmware/nixos/default`, retains
up to four older generations under `/boot/firmware/nixos`, and removes obsolete
generation directories.

Do not reboot unless the FAT contains the generated configuration, firmware,
and the default generation's direct kernel, initramfs, cmdline, overlays, and a
bcm2712 DTB. `config.txt` selects `nixos/default/` with `os_prefix`; all retained
generations live on the FAT, with no extlinux menu.

```bash
set -euo pipefail
sudo find /mnt/boot/firmware -maxdepth 2 -type f -print | sort
sudo test -s /mnt/boot/firmware/config.txt
sudo test -s /mnt/boot/firmware/nixos/default/cmdline.txt
sudo test -s /mnt/boot/firmware/nixos/default/kernel.img
sudo test -s /mnt/boot/firmware/nixos/default/initrd
sudo test -s /mnt/boot/firmware/nixos/default/system-link
sudo find /mnt/boot/firmware/nixos/default -maxdepth 1 \
  -name 'bcm2712*.dtb' -print -quit \
  | grep -q .
sudo find /mnt/boot/firmware/nixos/default/overlays -maxdepth 1 \
  -type f -print -quit | grep -q .
sudo grep -q '^kernel=kernel.img$' /mnt/boot/firmware/config.txt
sudo grep -q '^os_prefix=nixos/default/$' /mnt/boot/firmware/config.txt
sudo grep -q '^initramfs initrd followkernel$' /mnt/boot/firmware/config.txt
sudo sed -n '1,160p' /mnt/boot/firmware/config.txt
sudo find /mnt/boot/firmware/nixos -mindepth 1 -maxdepth 1 \
  -type d -print | sort
```

### Pinned-upstream deviation note

This host pins full rev `67616c24ed74573750f4864abfc358296a077466`, the
2026-08-01 default-branch tip inspected before configuration. Its source makes
6.18.39 the default matched kernel/firmware bundle; Pi 5 base supplies `nvme`
and `pcie_brcmstb` in the initrd; the generational `kernel` installer targets
`/boot/firmware`; and the separate Bluetooth module enables host BlueZ/krnbt.
No framework behavior in this runbook is an unverified assumption. The
controller still must regenerate `flake.lock` and pass the eval/kernel gates.
Never move the pin without re-reading the same modules and installer scripts.

## 8. Set EEPROM boot order

`0xf416` tries NVMe, then SD, then USB. Apply while still in the live rescue OS.

```bash
set -euo pipefail
sudo rpi-eeprom-config --edit
```

Set `BOOT_ORDER=0xf416`, save, then verify:

```bash
set -euo pipefail
sudo rpi-eeprom-config | grep '^BOOT_ORDER=0xf416$'
```

## 9. First NVMe boot

```bash
set -euo pipefail
sudo reboot
```

After reconnecting, prove the root device, vendor kernel, and populated default
generation. Refreshes happen as part of later `nixos-rebuild switch`/`boot`
generation activation, not through a firmware-migration service.

```bash
set -euo pipefail
findmnt -no SOURCE /
uname -r | grep '^6\.18\.39'
readlink -f /run/current-system/kernel
sudo test -s /boot/firmware/nixos/default/kernel.img
sudo test -s /boot/firmware/nixos/default/initrd
```

## 10. Cold-boot invariant

Shut down, remove the SD card physically, remove power for 30 seconds, then boot.
Repeat the root-device check. Store the labelled rescue card in a drawer; never
leave it in the slot, where a future boot-order fallback could start it.

## 11. Verify nftables with k3s networking and ServiceLB

Do this immediately after k3s first becomes ready. This host deliberately keeps
NixOS's nftables firewall while modern k3s uses the iptables-nft compatibility
path. Prove the combination rather than assuming it: the temporary pods below
exercise pod-to-pod forwarding and CoreDNS, then the ServiceLB check confirms
Traefik has a listener. Delete the temporary namespace even if a check fails.

```bash
set -euo pipefail
sudo k3s kubectl wait --for=condition=Available deployment/coredns \
  -n kube-system --timeout=300s
sudo k3s kubectl wait --for=condition=Ready pod \
  -n kube-system -l svccontroller.k3s.cattle.io/svcname=traefik \
  --timeout=300s
sudo k3s kubectl get pods -n kube-system \
  -l svccontroller.k3s.cattle.io/svcname=traefik -o name | grep -q .

check_ns=firewall-check
sudo k3s kubectl create namespace "$check_ns"
cleanup() { sudo k3s kubectl delete namespace "$check_ns" --wait=false; }
trap cleanup EXIT
sudo k3s kubectl run ping-target -n "$check_ns" \
  --image=busybox:1.37 --restart=Never -- sleep 600
sudo k3s kubectl wait -n "$check_ns" --for=condition=Ready \
  pod/ping-target --timeout=180s
target_ip="$(sudo k3s kubectl get pod ping-target -n "$check_ns" \
  -o jsonpath='{.status.podIP}')"
test -n "$target_ip"
sudo k3s kubectl run network-check -n "$check_ns" --rm -i \
  --image=busybox:1.37 --restart=Never --command -- \
  sh -ec "ping -c 3 '$target_ip'; nslookup kubernetes.default.svc.cluster.local"
```

From a different machine on the home LAN, curl Traefik's TCP 443 hostPort. A
403 is expected because the ingress middleware only admits Cloudflare source
ranges; any HTTP response proves that LAN-to-ServiceLB forwarding reached
Traefik. A timeout or refusal fails the firewall/ServiceLB check.

```bash
set -euo pipefail
curl --insecure --silent --show-error --output /dev/null \
  --write-out 'Traefik HTTP status: %{http_code}\n' \
  --resolve homeassistant.pelargir.saldivar.io:443:10.0.0.165 \
  https://homeassistant.pelargir.saldivar.io/
```

If these checks fail and packet traces isolate the host nftables firewall as
the cause, the documented fallback is a future config change—not an ad-hoc live
rule: set `networking.nftables.enable = false` to use the iptables backend, then
rewrite `wireguard.nix`'s nftables-only `extraForwardRules` as equivalent
`networking.firewall.extraCommands`/`extraStopCommands`. Do not flip the backend
without that WireGuard forward-ACL translation and a new eval/reachability test.

## 12. Pre-pull every immutable workload image

This list is the offline-restart contract and must stay identical to the
manifests. Pull before depending on the workloads.

```bash
set -euo pipefail
images='
docker.io/library/eclipse-mosquitto@sha256:212f89e1eaeb2c322d6441b64396e3346026674db8fa9c27beac293405c32b3c
docker.io/koenkk/zigbee2mqtt@sha256:4fb4db4d49a217bed6d0204f454ce809febc565bbe051b85c556ee2bcef73d8c
ghcr.io/home-assistant/home-assistant@sha256:c01d6c54679a1934a2bd62a8e3289cd1439b45b82dc5b2dba01793368c500908
docker.io/qmcgaw/ddns-updater@sha256:3e2aa558946b5a293def4d73008fa4651c072b2c12932cecd02126fb23979831
'
printf '%s\n' "$images" | while IFS= read -r image; do
  [ -n "$image" ] || continue
  sudo k3s ctr images pull "$image"
done
```

## 13. Restore and verify applications

Follow `ZIGBEE-RECOVERY.md` to create the coordinator seed, copy the HA snapshot
into its PVC, and add HA's trusted proxies. Then verify reconciliation:

```bash
set -euo pipefail
sudo k3s kubectl get nodes -o wide
sudo k3s kubectl get all,pvc -n home
sudo k3s kubectl wait --for=condition=Available deployment/mosquitto \
  -n home --timeout=300s
curl --fail --show-error http://127.0.0.1:8123/
```

Zigbee routers re-announce on their own. Wake each battery device with its normal
button, then rename all nine devices to their old friendly names before judging
HA entity continuity. HA first boots at 2024.11.1; do not jump directly to 2026.

## 14. Router and Cloudflare

Forward TCP 80/443 and UDP 51820 to pelargir's reserved LAN address. Replace the
`REPLACE-AT-DEPLOY` ACME email in `manifests/ingress.yaml` before rebuilding. The
HA record is an orange-cloud CNAME to `pelargir.saldivar.io`; that endpoint is the
only A record updated by DDNS and must remain grey-cloud/DNS-only for WireGuard.
An orange-cloud endpoint cannot carry WireGuard UDP.

The existing token is roughly 20 months old. Verify it before expecting ACME or
DDNS to work, and replace the sops value if Cloudflare reports it dead:

```bash
set -euo pipefail
read -r -s -p 'Cloudflare API token: ' cloudflare_token
printf '\n'
curl --fail --show-error --silent \
  -H "Authorization: Bearer ${cloudflare_token}" \
  https://api.cloudflare.com/client/v4/user/tokens/verify
unset cloudflare_token
```

## 15. Enable remote backup last

The restic repository has `initialize = false` on purpose. Complete every step
in `MINAS-PREP.md`, relight WireGuard, manually initialize the intended remote
repository once, and test a restore. Until then the timer's handshake/SSH
condition cleanly skips; this is not a failed backup and must not create an
empty repository at a typoed path.
