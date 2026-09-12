# imladris — destructive disk layout.
#
# ⛔ THE MICROSD IS THE ONLY THING DISKO MAY EVER TOUCH ON THIS HOST.
#
# The four NVMe drives in the USB enclosure are formatted BY HAND, once, through
# docs/runbooks/imladris/install.md — never by disko, and never by a module that
# "helpfully" declares them. The reason is concrete rather than stylistic. As of
# 2026-09-11 the bays hold:
#
#   0:0  CT4000P3PSSD8  2336E873EE7A     Proxmox pve LVM  — disposable
#   0:1  CT2000P3PSSD8  2345E8844F5D     EFI + APFS       — data to preserve
#   0:2  Samsung 970 EVO Plus 2TB  S6S2NS0T629854Y   exFAT  — unverified
#   0:3  Samsung 970 EVO Plus 2TB  S6S2NS0T629836M   LUKS   — NARDOL'S ROLLBACK
#
# Bay 0:3 is the only rollback for nardol's single-NVMe migration and
# docs/runbooks/nardol/single-nvme-migration.md requires it be retained
# unchanged until that migration's cold-boot acceptance completes. Bay 0:1 holds
# data that has not been copied anywhere yet.
#
# ⚠️ AND THE USUAL SAFETY RULE DOES NOT WORK HERE. Everywhere else in this repo
# the serial-qualified /dev/disk/by-id path is the safety boundary. Through this
# enclosure it is NOT: the ASM2464 bridge reports one fake serial
# (AAAABBBB0007) for all four bays, so `usb-ASMT_ASM246X_AAAABBBB0007-0:N`
# identifies the SLOT, not the disk. Move a drive to another bay and its by-id
# path follows the bay. A by-id path is therefore not sufficient evidence of
# which physical drive is about to be erased — `smartctl -d sntasmedia` is.
#
# Bays 0:2 and 0:3 make this concrete: same model string, and their serials
# share the first twelve characters (S6S2NS0T6298·54Y vs S6S2NS0T6298·36M).
{
  config,
  lib,
  ...
}:
let
  # Read off the live installer 2026-09-11: a 119.4 GB card, and the only mmc
  # device on this host. Re-read and update if the card is ever replaced —
  # `ls -l /dev/disk/by-id/ | grep mmc-` — because a stale value here means disko
  # fails closed on a missing device, which is the correct failure but an
  # annoying one to diagnose at install time.
  expectedDisk = "/dev/disk/by-id/mmc-GD2S5_0xec5057a0";

  # Evaluated as part of disko.devices itself, including when building
  # diskoScript. NixOS assertions alone do not protect that path — this is
  # nardol's guard, for the same reason.
  guardDisk =
    disk:
    if disk != expectedDisk then
      throw ''
        imladris/disko.nix: refusing unexpected target ${disk}
        The only permitted target is the microSD card, ${expectedDisk}.
        The four NVMe drives in the USB enclosure must remain outside disko.
      ''
    else
      disk;

  rootDisk = guardDisk expectedDisk;
in
{
  disko.devices.disk.sd = {
    device = rootDisk;
    type = "disk";
    content = {
      type = "gpt";
      partitions = {
        firmware = {
          priority = 1;
          # nixos-raspberrypi's "kernel" loader writes to its firmwarePath,
          # /boot/firmware by default; it does not look up a disk label. vfat is
          # required by the Pi GPU firmware. 1 GiB because that mode retains
          # several kernel/initrd/DTB generations — same sizing as pelargir.
          size = "1G";
          type = "EF00";
          content = {
            type = "filesystem";
            format = "vfat";
            extraArgs = [
              "-n"
              "FIRMWARE"
            ];
            mountpoint = "/boot/firmware";
            mountOptions = [ "umask=0077" ];
          };
        };

        root = {
          priority = 2;
          size = "100%";
          content = {
            type = "filesystem";
            format = "ext4";
            extraArgs = [
              "-L"
              "imladris-sd"
            ];
            mountpoint = "/";
            # noatime is not a micro-optimisation on a card: every read would
            # otherwise cost a metadata write, which is exactly the write
            # amplification that kills microSD. See ./system.nix for the rest of
            # the write-reduction posture, and ./storage.nix for why Jellyfin's
            # database deliberately does not live on this filesystem.
            mountOptions = [
              "noatime"
              "errors=remount-ro"
            ];
          };
        };
      };
    };
  };

  assertions = [
    {
      assertion = builtins.attrNames config.disko.devices.disk == [ "sd" ];
      message = "imladris: disko must declare exactly one disk, the microSD.";
    }
    {
      assertion = lib.hasPrefix "/dev/disk/by-id/mmc-" config.disko.devices.disk.sd.device;
      message = "imladris: the install target must be a microSD by-id path (mmc-*).";
    }
    {
      # The enclosure bays must never appear as a disko target, by any spelling.
      assertion = !(lib.hasInfix "ASM246X" config.disko.devices.disk.sd.device);
      message = "imladris: the USB NVMe enclosure must never be a disko target.";
    }
    {
      assertion = (config.disko.devices.zpool or { }) == { };
      message = "imladris: no zpool may be managed by disko.";
    }
  ];
}
