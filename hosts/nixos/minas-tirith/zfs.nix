# minas-tirith (raz-server) — ZFS.
#
# Host-local on purpose: ../../modules/zfs.nix is left untouched because it sets
# `boot.zfs.forceImportAll = true`, pins kernelPackages via the now-removed
# `latestCompatibleLinuxPackages`, and runs a hand-rolled `zpool import -a`
# unit. All three are wrong for this host — see below.
#
# The two pools already exist and MUST NOT be created, destroyed or reshaped by
# this configuration. Root is ext4-on-LUKS on the NVMe; ZFS is data-only here.
#
#   storage   raidz2  7 x 14TB   (~91 TB, ~91% full)
#   storage2  raidz1  2 x 8TB SSD
#
# Both survived the 100 unclean shutdowns that destroyed the old btrfs root,
# with zero errors — redundancy across independent devices is why.
{ ... }:
{
  boot.supportedFilesystems = [
    "ext4"
    "vfat"
    "zfs"
  ];

  # Named, per-pool imports. NixOS generates one import unit per entry, which
  # is what we want: deliberate and individually diagnosable.
  boot.zfs.extraPools = [
    "storage"
    "storage2"
  ];

  # DO NOT set these true.
  #
  # forceImportAll appends `-f` to every non-root pool import on every boot,
  # permanently defeating the hostid ownership check that stops two systems
  # importing the same pool and corrupting it. Two pools are not dangerous;
  # permanently forcing them is.
  #
  # The pools currently carry a hostid mismatch (label says hostid 678912 /
  # hostname 'raz-server'; the live system's hostid is 0149f5f3). The correct
  # fix is ZFS's own documented remedy, done once during the migration:
  #     old system:  zpool export storage && zpool export storage2
  #     new system:  zpool import storage  && zpool import storage2
  # A cleanly exported pool has no ownership claim, so it imports without -f
  # and records this host's id. After that the warning is gone for good and
  # nothing here ever needs forcing.
  boot.zfs.forceImportAll = false;
  boot.zfs.forceImportRoot = false;

  # Root is not on ZFS, so no `zfs load-key` / native-encryption plumbing.

  services.zfs.autoScrub = {
    enable = true;
    interval = "monthly";
  };

  # Both pools are SSD-backed or SSD-inclusive; TRIM is worth having.
  services.zfs.trim.enable = true;

  # ZED has NO delivery path here and is not the notification mechanism — be
  # clear about that rather than assuming it covers us. `enableMail = false` and
  # there is no MTA on this host, so ZED only writes locally; local logs on a
  # remote box nobody reads are not monitoring, which is the exact failure that
  # let the btrfs damage sit unnoticed.
  #
  # The actual alerting path for pool problems is the heartbeat in
  # ./monitoring.nix, which runs `zpool status -x` every 5 minutes and reports
  # outward. ZED's value here is the local event log and post-resilver scrub.
  services.zfs.zed.enableMail = false;
  services.zfs.zed.settings = {
    ZED_DEBUG_LOG = "/var/log/zed.debug.log";
    ZED_NOTIFY_VERBOSE = true;
    ZED_SCRUB_AFTER_RESILVER = true;
  };
}
