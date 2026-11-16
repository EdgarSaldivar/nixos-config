# edoras — destructive layout for the Intel 660p NVMe only (formerly in a
# HIKSEMI USB enclosure, which is how it is installed).
#
# `device` is the name the NVMe has once it sits in edoras's M.2 slot. The
# install itself runs on nardol with the drive in a USB enclosure, where that
# name does not exist, so docs/runbooks/edoras/install.md overrides it for that
# one run (`disko-install --disk root /dev/sdX`) after matching the USB device
# by size, model and serial.
#
# That override is why the serial pin is enforced by checks/edoras-disko-targets.nix,
# which evaluates the committed config, rather than by an assertion here: an
# assertion on `device` would also reject the runbook's override.
#
# No LUKS: nobody on site can answer a passphrase prompt (see ./secrets.nix).
{ config, ... }:
let
  rootDisk = "/dev/disk/by-id/nvme-INTEL_SSDPEKNW010T8_BTNH10350EER1P0B";
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
          size = "2G";
          type = "EF00";
          content = {
            type = "filesystem";
            format = "vfat";
            mountpoint = "/boot";
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
              "nixos-root"
              "-m"
              "1"
            ];
            mountpoint = "/";
            mountOptions = [
              "noatime"
              "errors=remount-ro"
              "nodiscard"
            ];
          };
        };
      };
    };
  };

  assertions = [
    {
      assertion = builtins.attrNames config.disko.devices.disk == [ "root" ];
      message = "edoras: disko must declare exactly the Intel 660p NVMe as disk.root";
    }
    {
      assertion = !(config.disko.devices ? zpool) || config.disko.devices.zpool == { };
      message = "edoras: disko must not declare a zpool";
    }
  ];
}
