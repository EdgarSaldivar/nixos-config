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

## 3. Trust the vendor-kernel binary cache

raspberry-pi-nix builds the Raspberry Pi vendor kernel and publishes it through
the nix-community cache. Add the same substituter and key declared by the flake
before evaluating it; a fresh installer does not silently trust flake-level Nix
configuration. The key below was verified from the Cachix API on 2026-08-03.

```bash
set -euo pipefail
cache_line='extra-substituters = https://nix-community.cachix.org'
key_line='extra-trusted-public-keys = nix-community.cachix.org-1:mB9FSh9qf2dCimDSUo8Zy7bkq5CX+/rkCWyvRCYg3Fs='
grep -qxF "$cache_line" /etc/nix/nix.conf \
  || printf '%s\n' "$cache_line" | sudo tee -a /etc/nix/nix.conf
grep -qxF "$key_line" /etc/nix/nix.conf \
  || printf '%s\n' "$key_line" | sudo tee -a /etc/nix/nix.conf
sudo systemctl restart nix-daemon.service
nix config show substituters | grep -F 'https://nix-community.cachix.org'
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

Build the aarch64 closure on the Pi. raspberry-pi-nix 3e8100d selects its
`linux-rpi` bcm2712 vendor package; the cache configured above should substitute
that kernel. There is no cross-build configuration in this host.

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

At pinned rev 3e8100d, raspberry-pi-nix refreshes the FAT with a systemd service.
That service cannot run before the first kernel exists, so invoke its generated
migration program once in the installed root. This is not a second firmware
implementation: it is the exact ExecStart produced by the imported module.

```bash
set -euo pipefail
unit_link=/mnt/etc/systemd/system/multi-user.target.wants/raspberry-pi-firmware-migrate.service
unit_target="$(readlink "$unit_link")"
case "$unit_target" in
  /nix/store/*) ;;
  *) printf 'Unexpected firmware unit target: %s\n' "$unit_target" >&2; exit 1 ;;
esac
unit="/mnt$unit_target"
test -s "$unit"
migrator="$(sed -n 's/^ExecStart=//p' "$unit")"
case "$migrator" in
  /nix/store/*) ;;
  *) printf 'Unexpected firmware migrator: %s\n' "$migrator" >&2; exit 1 ;;
esac
sudo install -d -o root -g root -m 0755 /mnt/var/lib/raspberrypi-firmware
sudo nixos-enter --root /mnt --command \
  "env STATE_DIRECTORY=/var/lib/raspberrypi-firmware $migrator"
```

Do not reboot unless the FAT contains the direct kernel, initramfs, generated
configuration, firmware, overlays, and a bcm2712 DTB. The FAT holds the current
direct-boot pair; NixOS's init-script loader records the current and older system
generations on the root filesystem rather than in an extlinux menu.

```bash
set -euo pipefail
sudo find /mnt/boot/firmware -maxdepth 2 -type f -print | sort
sudo test -s /mnt/boot/firmware/config.txt
sudo test -s /mnt/boot/firmware/cmdline.txt
sudo test -s /mnt/boot/firmware/kernel.img
sudo test -s /mnt/boot/firmware/initrd
sudo find /mnt/boot/firmware -maxdepth 1 -name 'bcm2712*.dtb' -print -quit \
  | grep -q .
sudo grep -q '^kernel=kernel.img$' /mnt/boot/firmware/config.txt
sudo grep -q '^ramfsfile=initrd$' /mnt/boot/firmware/config.txt
sudo grep -q '^ramfsaddr=-1$' /mnt/boot/firmware/config.txt
sudo test -x /mnt/sbin/init
sudo test -s /mnt/boot/init-other-configurations-contents.txt
sudo sed -n '1,160p' /mnt/boot/firmware/config.txt
sudo sed -n '1,160p' /mnt/boot/init-other-configurations-contents.txt
```

### Pinned upstream deviation

The latest tagged release, v0.4.1, still imports its SD-image module from the
board module and does not install an initrd for direct boot. This host therefore
pins development rev `3e8100d` (inspected in full before configuration), where
the normal module is separated from `sd-image`, the firmware label is explicit,
and direct boot installs both `kernel.img` and `initrd`. Revisit this deviation
when upstream publishes a newer release; never move the pin without re-reading
the module and confirming the FAT migration behavior.

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

After reconnecting, prove both the root device and vendor kernel:

```bash
set -euo pipefail
findmnt -no SOURCE /
uname -r
readlink -f /run/current-system/kernel | grep -- '-linux-rpi-'
systemctl --no-pager --full status raspberry-pi-firmware-migrate.service
```

## 10. Cold-boot invariant

Shut down, remove the SD card physically, remove power for 30 seconds, then boot.
Repeat the root-device check. Store the labelled rescue card in a drawer; never
leave it in the slot, where a future boot-order fallback could start it.

## 11. Pre-pull every immutable workload image

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

## 12. Restore and verify applications

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

## 13. Router and Cloudflare

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

## 14. Enable remote backup last

The restic repository has `initialize = false` on purpose. Complete every step
in `MINAS-PREP.md`, relight WireGuard, manually initialize the intended remote
repository once, and test a restore. Until then the timer's handshake/SSH
condition cleanly skips; this is not a failed backup and must not create an
empty repository at a typoed path.
