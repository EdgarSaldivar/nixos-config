{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:

# edoras's only disk is the Intel 660p NVMe, pinned by the name it has in the PC.
# The install runs with the drive in a USB enclosure on nardol and overrides
# the device for that one run (docs/runbooks/edoras/install.md), so the pin
# lives here, against the committed config, rather than as an assertion in
# hosts/nixos/edoras/disko.nix that the override would also trip.
let
  disks = nixosConfigurations.edoras.config.disko.devices.disk;
  devices = lib.mapAttrsToList (_: d: d.device) disks;
  expected = [ "/dev/disk/by-id/nvme-INTEL_SSDPEKNW010T8_BTNH10350EER1P0B" ];
  zpools = nixosConfigurations.edoras.config.disko.devices.zpool or { };
in
if devices != expected then
  throw "edoras disko would touch ${toString devices} — expected exactly ${toString expected}"
else if zpools != { } then
  throw "edoras declares disko.devices.zpool (${toString (builtins.attrNames zpools)}) — this host has no ZFS"
else
  pkgs.runCommand "edoras-disko-targets-ok" { } "touch $out"
