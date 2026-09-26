# Disk layout for nardol.
#
# SAFETY: the WD_BLACK SN850X is the only destructive target. The Samsung 970
# EVO Plus and Crucial P3 Plus must remain outside disko. Rerunning disko erases
# every partition on the WD, including both LUKS2 headers.
#
# The device is referenced by its full serial-qualified by-id path, never by an
# nvmeXnY name. PCIe enumeration order is not stable on this machine.
{
  config,
  lib,
  ...
}:
let
  expectedDisk = "/dev/disk/by-id/nvme-WD_BLACK_SN850X_4000GB_24160W802539";

  # This guard is evaluated as part of disko.devices itself, including when
  # building diskoScript. NixOS assertions alone do not protect that path.
  guardDisk =
    disk:
    if disk != expectedDisk then
      throw ''
        nardol/disko.nix: refusing unexpected target ${disk}
        The only permitted target is ${expectedDisk}.
        The Samsung 970 EVO Plus and Crucial P3 Plus must remain outside disko.
      ''
    else
      disk;

  rootDisk = guardDisk "/dev/disk/by-id/nvme-WD_BLACK_SN850X_4000GB_24160W802539";

  # Created only on the ephemeral installer and passed to nixos-anywhere. The
  # passphrase itself must never be committed or placed in the Nix store.
  passwordFile = "/tmp/nardol-disko-password";
in
{
  disko.devices.disk.root = {
    device = rootDisk;
    type = "disk";
    content = {
      type = "gpt";
      partitions = {
        ESP = {
          priority = 1;
          name = "ESP";
          label = "nardol-esp";
          type = "EF00";
          size = "1G";
          content = {
            type = "filesystem";
            format = "vfat";
            mountpoint = "/boot";
            mountOptions = [ "umask=0077" ];
          };
        };

        # A fixed root allocation prevents Docker/Wolf data from exhausting the
        # operating-system filesystem while leaving ample room for generations.
        root = {
          priority = 2;
          name = "root";
          label = "nardol-root-luks";
          size = "512G";
          content = {
            type = "luks";
            name = "nardol-root";
            inherit passwordFile;
            extraFormatArgs = [
              "--type"
              "luks2"
            ];
            settings.allowDiscards = true;
            content = {
              type = "filesystem";
              format = "ext4";
              mountpoint = "/";
              extraArgs = [
                "-L"
                "nardol-root"
                "-m"
                "1"
              ];
              mountOptions = [
                "noatime"
                "commit=5"
                "errors=remount-ro"
                "nodiscard"
              ];
            };
          };
        };

        fast = {
          priority = 3;
          name = "fast";
          label = "nardol-fast-luks";
          size = "100%";
          content = {
            type = "luks";
            name = "nardol-fast";
            inherit passwordFile;
            extraFormatArgs = [
              "--type"
              "luks2"
            ];
            settings.allowDiscards = true;
            content = {
              type = "filesystem";
              format = "ext4";
              mountpoint = "/srv";
              extraArgs = [
                "-L"
                "nardol-fast"
                "-m"
                "1"
              ];
              mountOptions = [
                "noatime"
                "commit=5"
                "errors=remount-ro"
                "nodiscard"
              ];
            };
          };
        };
      };
    };
  };

  assertions = [
    {
      assertion = builtins.attrNames config.disko.devices.disk == [ "root" ];
      message = "nardol: disko must declare exactly the WD_BLACK as disk.root.";
    }
    {
      assertion = config.disko.devices.disk.root.device == expectedDisk;
      message = "nardol: disko root target is not the approved WD_BLACK SN850X.";
    }
    {
      assertion = (config.disko.devices.zpool or { }) == { };
      message = "nardol: no zpool may be managed by disko.";
    }
    {
      assertion = lib.hasPrefix "/dev/disk/by-id/nvme-" config.disko.devices.disk.root.device;
      message = "nardol: the install target must use a stable NVMe by-id path.";
    }
  ];
}
