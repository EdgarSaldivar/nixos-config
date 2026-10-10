# nardol — the archive: a four-bay USB NVMe enclosure, pooled with mergerfs and
# served to the Mac over SMB.
#
# The enclosure moved here from imladris on 2026-10-10. Its four pool members
# keep the ext4 labels they were given there (imladris-d1 … imladris-d4); the
# label is how they are found, so it stays. The enclosure's 100G state
# partitions (imladris-state, imladris-state-o) are deliberately NOT mounted:
# they hold other services' state, which moves only if and when those services do.
#
# ⛔ THE ENCLOSURE IS OPTIONAL TO THIS HOST'S BOOT. Every mount here is `nofail`
# with a short device timeout. On imladris the opposite choice (no `nofail`, so a
# missing member stops the boot) left the host in emergency mode, off the network,
# after a power cut. nardol is the gaming and inference host; a missing USB box
# must cost the archive, never the machine.
#
# What still protects the archive from being served half-present:
#   - the union REQUIRES every member mount, so one missing drive leaves
#     /srv/archive unmounted rather than quietly smaller, and
#   - smbd is bound to the union mount, so Samba never exports the empty
#     directory underneath it, and stops if the union goes away at runtime.
{
  config,
  lib,
  pkgs,
  utils,
  ...
}:
let
  archiveRoot = "/srv/archive";
  members = [
    "d1"
    "d2"
    "d3"
    "d4"
  ];
  memberPath = m: "/mnt/pool/${m}";
  archiveMount = "${utils.escapeSystemdPath archiveRoot}.mount";
in
{
  system.fsPackages = [ pkgs.mergerfs ];

  fileSystems =
    lib.listToAttrs (
      map (
        m:
        lib.nameValuePair (memberPath m) {
          device = "/dev/disk/by-label/imladris-${m}";
          fsType = "ext4";
          options = [
            "noatime"
            "nodev"
            "nosuid"
            "errors=remount-ro"
            "nofail"
            # USB enumeration is not instant; past this, give up and boot on.
            "x-systemd.device-timeout=30s"
          ];
        }
      ) members
    )
    // {
      ${archiveRoot} = {
        device = lib.concatMapStringsSep ":" memberPath members;
        fsType = "fuse.mergerfs";
        options = [
          # New files go to a branch chosen by free space, proportionally, so
          # the drives fill evenly. `epmfs` pinned whole trees to one drive.
          "category.create=pfrd"
          # Must exceed the largest single file written.
          "minfreespace=100G"
          "moveonenospc=pfrd"
          "cache.files=off"
          "dropcacheonclose=false"
          "func.getattr=newest"
          # Samba's streams_xattr keeps Finder metadata in xattrs on the ext4.
          "xattr=passthrough"
          # SMB directory listings stat every entry; cache that across FUSE.
          "cache.readdir=true"
          "cache.entry=30"
          "cache.attr=30"
          "read-thread-count=0"
          "process-thread-count=-1"
          "fuse-msg-size=1M"
          "allow_other"
          "fsname=archive"
          "nofail"
        ]
        # A member that failed to mount leaves its mountpoint an empty directory;
        # requiring each mount keeps that from becoming a silent hole in the union.
        ++ map (m: "x-systemd.requires-mounts-for=${memberPath m}") members;
      };
    };

  # ---------------------------------------------------------------------------
  # Samba
  # ---------------------------------------------------------------------------
  # Every file on the pool is owned by uid 1000, which is edgar here as it was on
  # imladris, so authenticating as edgar is the whole permission model. Group
  # ownership is mostly gid 994 (imladris's `media`); that gid is unrelated on
  # this host and nothing here relies on it.
  #
  # edgar's SMB password lives in Samba's own database
  # (/var/lib/samba/private/passdb.tdb, on the root NVMe). It was carried over
  # from imladris on 2026-10-10, so existing Keychain entries keep working. This
  # host has no sops; set or rotate it with `sudo smbpasswd -a edgar`.
  services.samba = {
    enable = true;
    # ⛔ Never `openFirewall`: it opens 445 on every interface. The two
    # per-interface rules below are the whole access policy.
    openFirewall = false;
    # Standalone server, plain user accounts: no NetBIOS, no domain.
    nmbd.enable = false;
    winbindd.enable = false;

    settings = {
      global = {
        "server string" = "nardol";
        "netbios name" = "nardol";
        "security" = "user";
        "map to guest" = "never";
        "server min protocol" = "SMB3";

        # macOS. fruit:metadata=stream keeps Finder tags and comments in xattrs;
        # resource forks stay in `._name` AppleDouble files, which are METADATA
        # in this archive, not litter. Not a Time Machine target.
        "vfs objects" = "catia fruit streams_xattr";
        "fruit:metadata" = "stream";
        "fruit:veto_appledouble" = "no";
        "fruit:posix_rename" = "yes";
        "fruit:time machine" = "no";
        # Finder copies within the share happen server-side.
        "fruit:copyfile" = "yes";
      };

      archive = {
        path = archiveRoot;
        browseable = "yes";
        "read only" = "no";
        "valid users" = "edgar";
        "create mask" = "0664";
        "directory mask" = "0775";
      };
    };
  };

  systemd.services.samba-smbd = {
    requires = [ archiveMount ];
    after = [ archiveMount ];
    # BindsTo, not just Requires: if the enclosure drops at runtime the union
    # stops, and the export must stop with it.
    bindsTo = [ archiveMount ];
  };

  # LAN and tailnet only. No Avahi: Wolf already answers mDNS on this host for
  # Moonlight, and a second responder is how that discovery breaks. The Mac
  # reaches the share as smb://nardol/archive through the router's DNS.
  networking.firewall.interfaces.eth0.allowedTCPPorts = [ 445 ];
  networking.firewall.interfaces.tailscale0.allowedTCPPorts = [ 445 ];

  assertions = [
    {
      assertion = lib.all (fs: lib.elem "nofail" fs.options) (
        lib.filter (fs: lib.hasPrefix "/mnt/pool/" fs.mountPoint || fs.mountPoint == archiveRoot) (
          lib.attrValues config.fileSystems
        )
      );
      message = "nardol: every archive mount must be nofail, or a missing enclosure stops the boot.";
    }
  ];
}
