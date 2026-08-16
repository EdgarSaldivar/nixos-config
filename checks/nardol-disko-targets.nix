{ lib, pkgs, nixosConfigurations, darwinConfigurations, ... }:

# Nardol currently contains three NVMes. The serial-qualified Samsung
# 970 EVO Plus and WD SN850X are intentionally disposable during this
# migration; the Crucial P3 Plus must remain invisible to disko. Also
# pin both intended LUKS2 -> ext4 shapes mechanically.
  let
    disks = nixosConfigurations.nardol.config.disko.devices.disk;
    devices = lib.mapAttrs (_: d: d.device) disks;
    expected = {
      fast = "/dev/disk/by-id/nvme-WD_BLACK_SN850X_4000GB_24160W802539";
      root = "/dev/disk/by-id/nvme-Samsung_SSD_970_EVO_Plus_2TB_S6S2NS0T629836M";
    };
    zpools = nixosConfigurations.nardol.config.disko.devices.zpool or { };
    rootLuks = disks.root.content.partitions.root.content;
    fastLuks = disks.fast.content.partitions.fast.content;
    rootFs = rootLuks.content;
    fastFs = fastLuks.content;
    isLuks2 =
      luks:
      luks.type == "luks"
      &&
        luks.extraFormatArgs == [
          "--type"
          "luks2"
        ];
  in
  if
    builtins.attrNames disks != [
      "fast"
      "root"
    ]
  then
    throw "nardol disko must declare exactly the disks named fast and root"
  else if devices != expected then
    throw "nardol disko target set changed: ${builtins.toJSON devices}"
  else if zpools != { } then
    throw "nardol declares disko.devices.zpool (${toString (builtins.attrNames zpools)}) — no zpool may be managed"
  else if !isLuks2 rootLuks || !isLuks2 fastLuks then
    throw "nardol managed partitions must be explicitly formatted as LUKS2"
  else if
    rootLuks.name != "nardol-root"
    || rootFs.type != "filesystem"
    || rootFs.format != "ext4"
    || rootFs.mountpoint != "/"
  then
    throw "nardol root must be the nardol-root LUKS mapper containing ext4"
  else if
    fastLuks.name != "nardol-fast"
    || fastFs.type != "filesystem"
    || fastFs.format != "ext4"
    || fastFs.mountpoint != "/srv"
  then
    throw "nardol fast must be the nardol-fast LUKS mapper containing ext4 at /srv"
  else
    pkgs.runCommand "nardol-disko-targets-ok" { } "touch $out"
