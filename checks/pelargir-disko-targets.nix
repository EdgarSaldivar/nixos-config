{
  lib,
  pkgs,
  nixosConfigurations,
  darwinConfigurations,
  ...
}:

# Pelargir has one disposable install target, but the short nvme name is
# still unsafe: PCI discovery order can change across EEPROM/kernel
# updates. Keep the serial-qualified device mechanically pinned.
let
  disks = nixosConfigurations.pelargir.config.disko.devices.disk;
  devices = lib.mapAttrsToList (_: d: d.device) disks;
  expected = [ "/dev/disk/by-id/nvme-KINGSTON_SNVS1000G_50026B7685D2B59A" ];
  zpools = nixosConfigurations.pelargir.config.disko.devices.zpool or { };
in
if devices != expected then
  throw "pelargir disko would touch ${toString devices} — expected exactly ${toString expected}"
else if zpools != { } then
  throw "pelargir declares disko.devices.zpool (${toString (builtins.attrNames zpools)}) — this host has no disko-managed ZFS pools"
else
  pkgs.runCommand "pelargir-disko-targets-ok" { } "touch $out"
