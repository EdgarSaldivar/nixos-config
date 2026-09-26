# Nardol single-NVMe migration

This procedure moves Nardol from a Samsung root plus WD_BLACK `/srv` to the
serial-pinned WD_BLACK SN850X as its only installed drive. It is destructive to
the WD and must not begin until both backup copies pass the stopped-source gate.

## Fixed devices and target layout

| Role | Stable device |
| --- | --- |
| Destructive target | `/dev/disk/by-id/nvme-WD_BLACK_SN850X_4000GB_24160W802539` |
| Temporary source and rollback | `/dev/disk/by-id/nvme-Samsung_SSD_970_EVO_Plus_2TB_S6S2NS0T629836M` |
| Never touch | `/dev/disk/by-id/nvme-CT4000P3PSSD8_2336E873EE7A` |

The WD target is one GPT containing a 1 GiB ESP, a 512 GiB LUKS2/ext4 root,
and a LUKS2/ext4 `/srv` using the remaining space. The existing partlabels,
mapper names, filesystem labels, mountpoints, trim policy, and unlock contract
remain `nardol-esp`, `nardol-root-luks`/`nardol-root`, and
`nardol-fast-luks`/`nardol-fast`.

## Stop conditions

Do not erase the WD unless all of these are true:

- `nvme smart-log` and `smartctl -x` report no critical warning, media error,
  data-integrity error, or error-log entry for serial `24160W802539`.
- The current root and fast recovery passphrases pass
  `cryptsetup open --test-passphrase` through their by-partlabel paths.
- Tang, USB-key, initrd-SSH, and console recovery paths have been accounted for.
- `/srv` has two complete copies: one below
  `/var/lib/nardol-wd-migration/srv` on the Samsung and one below
  `/storage2/nardol-wd-migration-2026-09-10/srv` on Minas.
- Wolf and Docker are stopped and both copies pass a final `rsync --delete`
  followed by a checksum dry run against that stopped source.
- Nardol's non-declarative identity is captured: `/home`, stage-2 SSH host
  keys, the dedicated initrd SSH host key, `/etc/machine-id`, and
  `/var/lib/tailscale`.
- The committed flake passes `nix flake check`, its closure delta is nardol
  only, and evaluation shows the WD serial as the sole Disko device.

The Crucial must be physically disconnected before Disko runs. Put the Samsung
in a USB enclosure and verify the backup from the installer, then unmount,
close, and physically unplug it **before Disko runs**. The old Samsung and new
WD partitions intentionally use the same partlabels; leaving both attached
makes `/dev/disk/by-partlabel/*` ambiguous and can cause Disko to mount the
Samsung as the installation target even though the destructive disk itself is
serial-pinned. Reconnect the Samsung only after Disko and installation finish,
then mount it read-only as the restore source.

## Copy and verification contract

Use `rsync -aHAX --numeric-ids --one-file-system --delete` for each full and
final copy. After services stop, repeat each copy until a checksum dry run:

```sh
rsync -aHAXnc --numeric-ids --one-file-system --delete /srv/ DESTINATION/
```

produces no itemized changes and exits zero. Record source and destination
byte/file counts separately. Hash irreplaceable saves and configuration files
on each destination; arithmetic or metadata from the copy command alone is not
verification.

## Destructive install and restore

Run nixos-anywhere only from a clean, reviewed commit and use the repository's
pinned nixos-anywhere revision. Supply `/tmp/nardol-disko-password` at runtime
and the preserved dedicated initrd host key through `--extra-files`. Disko may
report exactly one target, the WD serial above.

After Disko and installation, but before reboot, reconnect the Samsung USB
enclosure and re-prove its underlying NVMe model/serial (USB bridge identity is
not sufficient). Then:

1. Unlock and mount the untouched Samsung root read-only.
2. Restore `/srv` from the Samsung copy with `rsync -aHAX --numeric-ids`.
3. Restore `/home`, stage-2 SSH host keys, `/etc/machine-id`, and Tailscale
   state with their original ownership and modes.
4. Re-enroll both new WD LUKS2 volumes with the existing Tang advertisement.
5. Add the unchanged raw USB-key material to both new LUKS2 volumes.
6. Verify the recovery passphrase with `cryptsetup open --test-passphrase`,
   inspect both bindings, back up both new LUKS2 headers off-host, and install
   systemd-boot to the WD ESP.

Do not alter the Samsung. If WD acceptance fails, shut down and boot the
Samsung ESP to return to the old root and its staged `/srv` copy.

## Acceptance and physical removal

With all drives still installed, boot the WD and prove with `findmnt` plus
`lsblk` that `/boot`, `/`, and `/srv` all resolve through partitions on serial
`24160W802539`. Verify `systemctl is-system-running`, failed units, SSH host-key
fingerprints, Tailscale identity, Docker's data root, Wolf, and selected game
saves.

Then shut down, remove the Samsung and Crucial, and cold-boot the WD alone.
Repeat the backing-device, unlock, service, and data checks. Retain the
Samsung unchanged until this cold-boot acceptance is complete.
