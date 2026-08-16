{ lib, pkgs, nixosConfigurations, darwinConfigurations, ... }:

# THE important one. disko destroys exactly the disks it is handed, so
# the only thing standing between a config edit and nine live ZFS pool
# members (~98 TB, no backup) is that this list stays correct. Assert it
# mechanically instead of re-reading it by eye before each install.
  let
    disks = nixosConfigurations.minas-tirith.config.disko.devices.disk;
    devices = lib.mapAttrsToList (_: d: d.device) disks;
    expected = [ "/dev/disk/by-id/nvme-Samsung_SSD_980_1TB_S64ANS0RA05335R" ];
    zpools = nixosConfigurations.minas-tirith.config.disko.devices.zpool or { };
  in
  if devices != expected then
    throw "minas-tirith disko would touch ${toString devices} — expected exactly ${toString expected}"
  else if zpools != { } then
    throw "minas-tirith declares disko.devices.zpool (${toString (builtins.attrNames zpools)}) — disko must never manage the existing pools"
  else
    pkgs.runCommand "minas-tirith-disko-targets-ok" { } "touch $out"
