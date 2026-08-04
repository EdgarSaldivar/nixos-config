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

## 3. Partition only the pinned NVMe

Clone this repository on the Pi and verify that the by-id symlink names the
Kingston drive before allowing disko to destroy the leftover bare ext4 layout.

```bash
set -euo pipefail
git clone REPLACE-WITH-REPOSITORY-URL nixos-config
cd nixos-config
readlink -f /dev/disk/by-id/nvme-KINGSTON_SNVS1000G_50026B7685D2B59A
sudo nix run github:nix-community/disko -- \
  --mode disko --flake .#pelargir
findmnt /mnt
findmnt /mnt/boot/firmware
```

## 4. Install natively

Build the aarch64 closure on the Pi. `linuxPackages_latest` should substitute
from `cache.nixos.org`; there is no cross-build configuration in this host.

```bash
set -euo pipefail
sudo nixos-install --root /mnt --flake .#pelargir --no-root-passwd
```

## 5. Place the pre-generated host identity before first boot

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

## 6. Hard boot-content gate

Do not reboot unless the FAT contains `config.txt`, `u-boot.bin`, the start/fixup
set, overlays, and a bcm2712 DTB, and extlinux points to paths that exist.

```bash
set -euo pipefail
sudo find /mnt/boot/firmware -maxdepth 2 -type f -print | sort
sudo test -s /mnt/boot/firmware/config.txt
sudo test -s /mnt/boot/firmware/u-boot.bin
sudo find /mnt/boot/firmware -maxdepth 1 -name 'bcm2712*.dtb' -print -quit \
  | grep -q .
sudo test -s /mnt/boot/firmware/extlinux/extlinux.conf
sudo sed -n '1,200p' /mnt/boot/firmware/extlinux/extlinux.conf
```

### Upstream U-Boot/NVMe deviation

The bumped nixos-hardware module (`2e790b0`, inspected in the Nix store) now
correctly populates firmware, but its own Pi README says its generic
`ubootRaspberryPiAarch64` cannot yet drive Pi 5 PCIe/NVMe. That conflicts with
this NVMe/extlinux design. Before sacrificing the rescue boot, stop at the
U-Boot console and prove that `nvme scan` and loading the extlinux config work.
If they do not, do **not** reboot into an unproven chain and do not silently
switch to direct kernel boot; record the module deviation for the controller.

## 7. Set EEPROM boot order

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

## 8. First NVMe boot

```bash
set -euo pipefail
sudo reboot
```

After reconnecting, prove both root device and mainline kernel:

```bash
set -euo pipefail
findmnt -no SOURCE /
uname -r
nix eval --raw nixpkgs#linuxPackages_latest.kernel.version
```

## 9. Cold-boot invariant

Shut down, remove the SD card physically, remove power for 30 seconds, then boot.
Repeat the root-device check. Store the labelled rescue card in a drawer; never
leave it in the slot, where a future boot-order fallback could start it.

## 10. Pre-pull every immutable workload image

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

## 11. Restore and verify applications

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

## 12. Router and Cloudflare

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

## 13. Enable remote backup last

The restic repository has `initialize = false` on purpose. Complete every step
in `MINAS-PREP.md`, relight WireGuard, manually initialize the intended remote
repository once, and test a restore. Until then the timer's handshake/SSH
condition cleanly skips; this is not a failed backup and must not create an
empty repository at a typoed path.
