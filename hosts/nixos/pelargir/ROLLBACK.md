# Pelargir rollback and no-boot recovery

Pelargir uses Raspberry Pi GPU-firmware direct kernel boot. There is no GRUB,
systemd-boot, or extlinux menu: `/boot/firmware/config.txt` points at
`nixos/default/`, so a person at the console cannot select an older generation.
Do not reboot after a rebuild until SSH, k3s, the watchdog, and the generated
files under `/boot/firmware/nixos/default/` have been checked.

## A bad activation while a shell still works

Keep the current SSH/console shell open and roll the system profile back:

```sh
sudo nixos-rebuild switch --rollback
readlink -f /run/current-system
sudo test -s /boot/firmware/nixos/default/kernel.img
sudo test -s /boot/firmware/nixos/default/initrd
sudo cat /boot/firmware/nixos/default/system-link
```

The `switch` action both activates the previous system generation and reruns the
Raspberry Pi bootloader installer, repopulating `nixos/default/`. Open a second
SSH session and verify services before closing the first one or rebooting. If a
network change broke new connections but the original shell remains alive, use
that original shell for the rollback.

## The box will not boot: select a retained generation from rescue microSD

The pinned nixos-raspberrypi `kernel` installer retains up to four numbered
generation directories alongside `nixos/default/`. Their names are
`<generation>-default`, and each contains a `system-link` text file identifying
the corresponding Nix store system. The firmware module explicitly supports
temporarily selecting one with `os_prefix=nixos/<generation>-default/`.

1. Power pelargir off. Insert the known-good aarch64 NixOS rescue microSD and
   boot it with the NVMe still attached. Do not run disko or format anything.
2. Become root and identify the Kingston drive and its partitions by stable ID:

   ```sh
   sudo -i
   ls -l /dev/disk/by-id/nvme-KINGSTON_SNVS1000G_50026B7685D2B59A*
   lsblk -f
   mkdir -p /mnt/pelargir-firmware
   mount /dev/disk/by-id/nvme-KINGSTON_SNVS1000G_50026B7685D2B59A-part1 \
     /mnt/pelargir-firmware
   ```

   Confirm that this is the vfat `FIRMWARE` filesystem before editing it:

   ```sh
   findmnt -no SOURCE,FSTYPE,LABEL /mnt/pelargir-firmware
   test -s /mnt/pelargir-firmware/config.txt
   find /mnt/pelargir-firmware/nixos -mindepth 1 -maxdepth 1 -type d -print | sort
   for f in /mnt/pelargir-firmware/nixos/*/system-link; do echo "$f: $(cat "$f")"; done
   ```

3. Choose a numbered generation that predates the bad change. Verify it has all
   direct-boot payloads before selecting it:

   ```sh
   good=123-default                 # replace 123 with the chosen generation
   dir=/mnt/pelargir-firmware/nixos/$good
   test -s "$dir/kernel.img"
   test -s "$dir/initrd"
   test -s "$dir/cmdline.txt"
   test -s "$dir/system-link"
   find "$dir" -maxdepth 1 -name '*.dtb' -print -quit | grep -q .
   test -d "$dir/overlays"
   ```

4. Back up and change only the generated `os_prefix` line:

   ```sh
   # This is FAT; plain cp avoids unsupported ownership/mode preservation.
   cp /mnt/pelargir-firmware/config.txt \
     /mnt/pelargir-firmware/config.txt.pre-rollback
   sed -i "s|^os_prefix=.*|os_prefix=nixos/$good/|" \
     /mnt/pelargir-firmware/config.txt
   grep '^os_prefix=' /mnt/pelargir-firmware/config.txt
   sync
   umount /mnt/pelargir-firmware
   poweroff
   ```

5. Remove the rescue card and power on. The NVMe firmware should now load the
   selected old kernel/initrd directly. Once the host is reachable, make that
   rollback durable:

   ```sh
   sudo nixos-rebuild switch --rollback
   grep '^os_prefix=nixos/default/$' /boot/firmware/config.txt
   readlink -f /run/current-system
   sudo cat /boot/firmware/nixos/default/system-link
   ```

The direct `os_prefix` selection above follows the pinned framework's documented
generation layout and builder source. The complete rescue-card procedure has
**not yet been exercised end-to-end on pelargir hardware**; treat the first drill
as a verification task and keep the original `config.txt` backup until the host
has booted successfully.

## If no retained firmware generation is usable

This fallback is **UNVERIFIED ON PELARGIR**. Use it only when the numbered
firmware directories are missing/damaged but a known-good system generation is
still present in the NVMe Nix store.

```sh
sudo -i
mount /dev/disk/by-id/nvme-KINGSTON_SNVS1000G_50026B7685D2B59A-part2 /mnt
mount /dev/disk/by-id/nvme-KINGSTON_SNVS1000G_50026B7685D2B59A-part1 /mnt/boot/firmware
mount --rbind /dev /mnt/dev
mount --make-rslave /mnt/dev
mount --rbind /proc /mnt/proc
mount --make-rslave /mnt/proc
mount --rbind /sys /mnt/sys
mount --make-rslave /mnt/sys
mount --rbind /run /mnt/run
mount --make-rslave /mnt/run
nixos-enter --root /mnt
ls -l /nix/var/nix/profiles/system-*-link
```

Inside `nixos-enter`, choose a known-good `system-N-link`, point the system
profile at it, and ask that generation to install itself for the next boot:

```sh
good=/nix/var/nix/profiles/system-123-link   # replace 123
test -x "$good/bin/switch-to-configuration"
nix-env --profile /nix/var/nix/profiles/system --set "$good"
/nix/var/nix/profiles/system/bin/switch-to-configuration boot
test -s /boot/firmware/nixos/default/kernel.img
cat /boot/firmware/nixos/default/system-link
exit
```

Then unmount in reverse order, power off, remove the rescue card, and boot from
NVMe. Do not claim this path works until it has been rehearsed with the actual
rescue image: `nixos-enter`, device discovery, and the Pi bootloader installer
must all be present and functional in that environment.
