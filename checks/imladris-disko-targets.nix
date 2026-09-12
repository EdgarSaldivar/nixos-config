{
  lib,
  pkgs,
  nixosConfigurations,
  darwinConfigurations,
  ...
}:

# imladris installs to a microSD and nothing else. The four NVMe drives in its
# USB enclosure are formatted by hand, once, through the install runbook.
#
# ⛔ THIS CHECK MATTERS MORE HERE THAN THE DISK COUNT SUGGESTS, because the
# safety property the rest of the fleet relies on does not exist on this host.
# Everywhere else, a /dev/disk/by-id path names one physical device. Through the
# ASM2464 bridge it does not: all four bays report the same fake serial
# (AAAABBBB0007), so `usb-ASMT_ASM246X_AAAABBBB0007-0:N` identifies the SLOT.
# A by-id path is therefore not evidence of which drive is about to be erased.
#
# As of 2026-09-11 two of those bays held data that exists nowhere else — an
# APFS volume not yet copied off, and nardol's ONLY single-NVMe migration
# rollback. A disko run that reached the enclosure would be unrecoverable, so the
# fence is asserted mechanically rather than re-read by eye before each install.
let
  cfg = nixosConfigurations.imladris.config;
  disks = cfg.disko.devices.disk;
  names = builtins.attrNames disks;
  devices = lib.mapAttrsToList (_: d: d.device) disks;
  zpools = cfg.disko.devices.zpool or { };

  # Any spelling of the enclosure, in any declared device string.
  enclosureMarkers = [
    "ASM246X"
    "ASMT"
    "AAAABBBB0007"
  ];
  touchesEnclosure = lib.any (dev: lib.any (m: lib.hasInfix m dev) enclosureMarkers) devices;

  # ⚠️ Deliberately a CLASS check, not an exact-serial pin — unlike every other
  # *-disko-targets check in this repo. Those hosts install to one disk among
  # several candidates, some holding irreplaceable data, so pinning the exact
  # serial is the safety property. Here the microSD is the ONLY mmc device and is
  # deliberately disposable; the danger is not "the wrong card", it is "the
  # enclosure", which the marker check above rejects outright. Pinning a serial
  # would only add a second file to edit every time a worn card is replaced.
  onlyMicroSD = lib.all (dev: lib.hasPrefix "/dev/disk/by-id/mmc-" dev) devices;
in
if names != [ "sd" ] then
  throw "imladris disko must declare exactly one disk named 'sd' — found ${toString names}"
else if touchesEnclosure then
  throw ''
    imladris disko names the USB NVMe enclosure: ${toString devices}
    The enclosure must NEVER be a disko target. Its bays are formatted by hand
    through docs/runbooks/imladris/install.md, after confirming the drive by its
    real NVMe serial with `smartctl -d sntasmedia` — the bridge's by-id path
    identifies the bay, not the disk.
  ''
else if !onlyMicroSD then
  throw "imladris disko target must be a microSD by-id path (mmc-*) — found ${toString devices}"
else if zpools != { } then
  throw "imladris declares disko.devices.zpool (${toString (builtins.attrNames zpools)}) — this host has no disko-managed ZFS pools"
else if cfg.services.k3s.enable then
  throw "imladris must not enable k3s — fault containment from the control plane is this host's entire purpose"
else
  pkgs.runCommand "imladris-disko-targets-ok" { } "touch $out"
