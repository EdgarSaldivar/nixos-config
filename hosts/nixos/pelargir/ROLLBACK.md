# Pelargir rollback and no-boot recovery

Pelargir uses Raspberry Pi GPU-firmware direct kernel boot. There is no GRUB,
systemd-boot, or extlinux menu: `/boot/firmware/config.txt` points at
`nixos/default/`, so a person at the console cannot select an older generation.
Do not reboot after a rebuild until SSH, k3s, the watchdog, and the generated
files under `/boot/firmware/nixos/default/` have been checked.

## ⛔ FIRST: is Kubernetes secret encryption enabled?

**Check before rolling back anything.** This is not optional and it is not a footnote —
it changes whether the procedure below is safe.

```sh
sudo k3s secrets-encrypt status
```

If that reports **`Enabled`**, then rolling the system profile back to a generation from
**before encryption was enabled silently removes `--secrets-encryption` from the k3s
server arguments** — and the datastore still contains encrypted Secrets. k3s then cannot
decrypt them. The result is a **cluster-wide Secret read outage**: pod volume mounts
fail, cert-manager fails, reflector fails, and every home pod that restarts stays down.

The failure is nasty for two reasons. It is *silent* — the rollback succeeds, the system
activates, k3s starts — and it is *delayed*, because already-running pods keep their
mounted Secrets and only break when something restarts. So the rollback looks like it
worked, and the damage surfaces later, apparently unrelated.

**If you must roll back a generation older than the encryption change:**

1. Roll back as normal, then **immediately re-add the flag** to the k3s server args and
   restart k3s, before anything restarts. Preserve the encryption config —
   do **not** delete `/var/lib/rancher/k3s/server/cred/encryption-config.json`.
2. Or, if Secrets are already unreadable: restore the datastore + matching server token
   from backup, per the recovery documented alongside the backups. There is **no**
   transactional rollback of encryption — removing the flag while encrypted rows exist
   is what causes the outage, not what fixes it.

The supported way to genuinely turn encryption off is `k3s secrets-encrypt disable`
followed by `rotate-keys`, which rewrites every Secret back to plaintext **first**. It is
a live procedure, not a rollback.

## A bad activation while a shell still works

Keep the current SSH/console shell open and roll the system profile back.

⛔ **`nixos-rebuild --rollback` does NOT work on this fleet.** It needs a channel,
and this is a pure flake deployment with none. It fails rather than rolling back,
which is the worst possible moment to discover a recovery command is wrong. Drive
the system profile directly instead:

```sh
# 1. List the retained generations and pick the last known-good N.
sudo nix-env --list-generations -p /nix/var/nix/profiles/system

# 2. Point the system profile at it.
sudo nix-env --switch-generation N -p /nix/var/nix/profiles/system

# 3. Activate it. This is the step that also reruns the Raspberry Pi bootloader
#    installer and repopulates nixos/default/.
sudo /nix/var/nix/profiles/system/bin/switch-to-configuration switch

# 4. Verify the activation landed AND that the boot artefacts were repopulated.
readlink -f /run/current-system
sudo test -s /boot/firmware/nixos/default/kernel.img
sudo test -s /boot/firmware/nixos/default/initrd
sudo cat /boot/firmware/nixos/default/system-link
```

Step 3 is what makes the rollback survive a reboot; step 2 alone only moves a
symlink. Do not skip the step-4 checks — an activation that succeeds while
`/boot/firmware/nixos/default/` stays empty leaves a machine that cannot boot, and
that failure mode has happened here during installation.

Open a second SSH session and verify services before closing the first one or
rebooting. If a network change broke new connections but the original shell
remains alive, use that original shell for the rollback.

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
   # Same reason as above: --rollback needs a channel this flake does not have.
   sudo nix-env --list-generations -p /nix/var/nix/profiles/system
   sudo nix-env --switch-generation N -p /nix/var/nix/profiles/system
   sudo /nix/var/nix/profiles/system/bin/switch-to-configuration switch
   grep '^os_prefix=nixos/default/$' /boot/firmware/config.txt
   readlink -f /run/current-system
   sudo cat /boot/firmware/nixos/default/system-link
   ```

   Pick the same generation `N` you selected by `os_prefix` on the rescue card,
   so the booted system and the system profile agree. The `os_prefix` grep is the
   check that activation put the firmware back on `nixos/default/`; until it does,
   the machine is still depending on the hand-edited rescue-card value.

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
