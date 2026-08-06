# osgiliath — destructive layout for the internal NVMe only.
#
# The external Sabrent SSD contains the existing Frigate recordings. It is
# mounted by hardware-services.nix and must never appear anywhere in this tree.
{ config, ... }:
let
  expectedRootDisk = "/dev/disk/by-id/nvme-AirDisk_1TB_SSD_QEK975R001121P1129";
  rootDisk =
    let
      candidate = "/dev/disk/by-id/nvme-AirDisk_1TB_SSD_QEK975R001121P1129";
    in
    if candidate == expectedRootDisk then
      candidate
    else
      throw "osgiliath/disko.nix: refusing any target except the serial-pinned AirDisk NVMe";
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
      message = "osgiliath: disko must declare exactly the internal NVMe as disk.root";
    }
    {
      assertion = config.disko.devices.disk.root.device == expectedRootDisk;
      message = "osgiliath: disko target is not the serial-pinned AirDisk NVMe";
    }
    {
      assertion = !(config.disko.devices ? zpool) || config.disko.devices.zpool == { };
      message = "osgiliath: disko must not declare a zpool";
    }
  ];
}
