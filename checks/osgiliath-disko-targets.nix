{ lib, pkgs, nixosConfigurations, darwinConfigurations, ... }:

# Osgiliath's external Sabrent SSD contains existing Frigate recordings
# and must remain outside disko. Only the serial-pinned internal NVMe is
# an installation target, and this host has no disko-managed ZFS pool.
  let
    disks = nixosConfigurations.osgiliath.config.disko.devices.disk;
    devices = lib.mapAttrsToList (_: d: d.device) disks;
    expected = [ "/dev/disk/by-id/nvme-AirDisk_1TB_SSD_QEK975R001121P1129" ];
    zpools = nixosConfigurations.osgiliath.config.disko.devices.zpool or { };
  in
  if devices != expected then
    throw "osgiliath disko would touch ${toString devices} — expected exactly ${toString expected}"
  else if zpools != { } then
    throw "osgiliath declares disko.devices.zpool (${toString (builtins.attrNames zpools)}) — this host has no disko-managed ZFS pools"
  else
    pkgs.runCommand "osgiliath-disko-targets-ok" { } "touch $out"
