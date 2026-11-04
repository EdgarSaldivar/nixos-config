{
  lib,
  pkgs,
  nixosConfigurations,
  darwinConfigurations,
  ...
}:

# Nardol's serial-qualified WD SN850X is the only destructive target. The
# Samsung 970 EVO Plus and Crucial P3 Plus must remain invisible to disko. Pin
# the one intended LUKS2 -> ext4 shape mechanically: ESP plus an encrypted root
# filling the rest of the disk, with /srv a plain directory on it.
let
  disks = nixosConfigurations.nardol.config.disko.devices.disk;
  devices = lib.mapAttrs (_: d: d.device) disks;
  expected = {
    root = "/dev/disk/by-id/nvme-WD_BLACK_SN850X_4000GB_24160W802539";
  };
  zpools = nixosConfigurations.nardol.config.disko.devices.zpool or { };
  rootLuks = disks.root.content.partitions.root.content;
  partitions = disks.root.content.partitions;
  rootFs = rootLuks.content;
  isLuks2 =
    luks:
    luks.type == "luks"
    &&
      luks.extraFormatArgs == [
        "--type"
        "luks2"
      ];
in
if builtins.attrNames disks != [ "root" ] then
  throw "nardol disko must declare exactly the WD_BLACK as disk.root"
else if devices != expected then
  throw "nardol disko target set changed: ${builtins.toJSON devices}"
else if zpools != { } then
  throw "nardol declares disko.devices.zpool (${toString (builtins.attrNames zpools)}) — no zpool may be managed"
else if
  builtins.sort builtins.lessThan (builtins.attrNames partitions) != [
    "ESP"
    "root"
  ]
then
  throw "nardol disko must declare exactly the ESP and the encrypted root"
else if partitions.root.size != "100%" then
  throw "nardol root must fill the disk, so all free space is one pool"
else if !isLuks2 rootLuks then
  throw "nardol managed partitions must be explicitly formatted as LUKS2"
else if
  rootLuks.name != "nardol-root"
  || rootFs.type != "filesystem"
  || rootFs.format != "ext4"
  || rootFs.mountpoint != "/"
then
  throw "nardol root must be the nardol-root LUKS mapper containing ext4"
else
  pkgs.runCommand "nardol-disko-targets-ok" { } "touch $out"
