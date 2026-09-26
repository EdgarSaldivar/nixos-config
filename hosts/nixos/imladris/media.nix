# imladris — SMB to the Mac, Jellyfin to everything else.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  archiveRoot = "/srv/archive";
  stateRoot = "/var/lib/imladris";
in
{
  # A shared group is how jellyfin reads files that edgar wrote over SMB. The
  # union carries `allow_other`, but the underlying ext4 permissions still apply.
  users.groups.media = { };
  users.users.edgar.extraGroups = [ "media" ];

  # ---------------------------------------------------------------------------
  # Samba
  # ---------------------------------------------------------------------------
  services.samba = {
    enable = true;

    # ⛔ Never `openFirewall`. That opens 445 on every interface including the
    # one facing the LAN's untrusted corners; the per-interface rules at the
    # bottom of this file are the whole access policy.
    openFirewall = false;

    # NetBIOS name resolution is legacy and macOS does not use it — Avahi below
    # is what makes this host appear in Finder's sidebar. Leaving nmbd off
    # removes a broadcast service that would answer on every interface.
    nmbd.enable = false;

    settings = {
      global = {
        "workgroup" = "WORKGROUP";
        "server string" = "imladris";
        "netbios name" = "imladris";
        "security" = "user";

        # No anonymous access, ever. This shares a personal archive.
        "guest account" = "nobody";
        "map to guest" = "never";

        # SMB1 is not a compatibility nicety, it is a liability, and no macOS
        # version in service needs it.
        "server min protocol" = "SMB3";
        "client min protocol" = "SMB3";

        # Bind to the interfaces we actually serve, rather than relying solely on
        # the firewall. Defence in depth: a firewall rule edited in error should
        # not be enough to expose this.
        "interfaces" = "lo lan0 tailscale0";
        "bind interfaces only" = "yes";

        # macOS interoperability.
        #
        # ⚠️ What this does and does not do, stated precisely — an earlier
        # version of this comment claimed it avoided AppleDouble files entirely,
        # which is wrong.
        #
        #   fruit:metadata=stream  puts Finder metadata (tags, colour labels,
        #   comments) into extended attributes via streams_xattr, which reach the
        #   underlying ext4. mergerfs passes xattrs through; ../storage.nix states
        #   that dependency explicitly.
        #
        #   fruit:resource is NOT set, so it keeps its default of `file`. Actual
        #   RESOURCE FORKS therefore still land in `._name` AppleDouble sidecars,
        #   and fruit:veto_appledouble=no leaves them visible. That is deliberate:
        #   Samba warns that typical filesystem xattr size limits are too small to
        #   hold substantial resource forks, so routing them through
        #   streams_xattr instead is not a clean escape.
        #
        # Consequence for operators: `._*` files in this archive are METADATA,
        # not litter. Deleting them destroys preserved resource forks silently.
        #
        # fruit:encoding is deliberately left at default rather than `native`:
        # Samba documents that `native` is not known to work fully alongside
        # fruit:metadata=stream.
        "vfs objects" = "catia fruit streams_xattr";
        "fruit:metadata" = "stream";
        "fruit:veto_appledouble" = "no";
        "fruit:posix_rename" = "yes";
        # ⛔ Deliberately NOT a Time Machine target. Time Machine writes
        # continuously and sparsebundle-shaped, which is the opposite of this
        # pool's append-mostly archive workload — and it would quietly make a
        # backup destination out of storage that has no backup of its own.
        "fruit:time machine" = "no";
      };

      # ── The archive: WRITABLE. Operator decision, 2026-09-11. ─────────────
      #
      # This was briefly read-only, paired with a separate `_ingest` drop share
      # and a promote step. That was rejected as daily friction, and the split
      # share was removed with it — an ingest area only exists to be the writable
      # half of a read-only archive.
      #
      # ⚠️ What that means, recorded so it is a known risk rather than a
      # forgotten one: a Finder window now has delete permission over this
      # archive, and as of this writing the archive has exactly ONE copy. There
      # is no parity by design, no snapshots, and the restic push to minas-tirith
      # is declared in ../secrets.nix but NOT commissioned — that host's pool was
      # ~91% full on 2026-09-11.
      #
      # So the sole protection against an accidental ⌘⌫ or a bad script is
      # currently the operator. Commissioning backup and proving a restore is
      # what actually retires this risk; see the runbook's outstanding work.
      archive = {
        path = archiveRoot;
        browseable = "yes";
        "read only" = "no";
        "valid users" = "edgar";
        "force group" = "media";
        "create mask" = "0664";
        "directory mask" = "0775";
      };
    };
  };

  # Samba must not start before the union is assembled and proven, or it would
  # export an empty directory on the microSD that looks like the archive and
  # accepts writes into it.
  systemd.services.samba-smbd = {
    requires = [ "imladris-storage.target" ];
    after = [ "imladris-storage.target" ];
    # BindsTo propagates the STOP, which Requires alone does not. If a USB knock
    # or enclosure reset takes a pool member away at runtime, ../storage.nix's
    # target goes down and this export must go with it — rather than continuing
    # to serve a silently incomplete archive.
    bindsTo = [ "imladris-storage.target" ];
  };

  # ---------------------------------------------------------------------------
  # The SMB password, applied from sops rather than typed once.
  # ---------------------------------------------------------------------------
  # Samba keeps its own password database (a tdb under /var/lib/samba/private)
  # and does NOT read /etc/shadow, so `users.users.edgar.hashedPasswordFile`
  # does nothing for the share. The runbook used to say "run `smbpasswd -a edgar`
  # once" — which works until the moment it doesn't: that tdb lives on the
  # microSD, so reimaging the card, or restoring this host from the flake alone,
  # leaves Samba running and rejecting every login with no configuration
  # anywhere in the repo explaining why.
  #
  # Applying it on every boot from the sops secret makes the share reproducible
  # from the flake plus secrets/imladris.yaml, which is the standard the rest of
  # the fleet is held to. The operation is idempotent — smbpasswd -a on an
  # existing user updates the password rather than failing.
  #
  # mkIf on the secret's presence keeps this file evaluable while ./secrets.nix
  # is still an un-imported commented-out line: no secret, no unit, no eval
  # error. It starts working the moment that import lands.
  systemd.services.imladris-samba-password = lib.mkIf (config.sops.secrets ? samba_password) {
    description = "Apply edgar's SMB password from sops";
    wantedBy = [ "multi-user.target" ];
    # After smbd so Samba has already created its private directory; the
    # brief window where smbd is up with a stale password is harmless, and
    # far better than racing the directory's creation.
    after = [
      "sops-install-secrets.service"
      "samba-smbd.service"
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      pw="$(cat ${config.sops.secrets.samba_password.path})"
      # -s reads from stdin, which wants the password twice; -a adds or
      # updates. Nothing is passed on the command line, where it would be
      # visible in /proc to every user on the box.
      printf '%s\n%s\n' "$pw" "$pw" \
        | ${config.services.samba.package}/bin/smbpasswd -a -s edgar
    '';
  };

  # ---------------------------------------------------------------------------
  # Avahi — what actually puts this host in Finder's sidebar.
  # ---------------------------------------------------------------------------
  services.avahi = {
    enable = true;
    nssmdns4 = true;
    openFirewall = false;
    publish = {
      enable = true;
      userServices = true;
    };
    extraServiceFiles.smb = ''
      <?xml version="1.0" standalone='no'?><!--*-nxml-*-->
      <!DOCTYPE service-group SYSTEM "avahi-service.dtd">
      <service-group>
        <name replace-wildcards="yes">%h</name>
        <service>
          <type>_smb._tcp</type>
          <port>445</port>
        </service>
        <service>
          <type>_device-info._tcp</type>
          <port>0</port>
          <txt-record>model=RackMac</txt-record>
        </service>
      </service-group>
    '';
  };

  # ---------------------------------------------------------------------------
  # Jellyfin
  # ---------------------------------------------------------------------------
  services.jellyfin = {
    enable = true;
    openFirewall = false;
    group = "media";

    # ⛔ ALL THREE ON NVMe, NOT ON THE microSD.
    #
    # Jellyfin's SQLite database writes on every playback, scan and metadata
    # update, and a library scan writes heavily. That workload on a card is both
    # the classic microSD killer and the usual reason "Jellyfin feels sluggish on
    # a Pi". /var/lib/imladris is its own labelled ext4 partition on NVMe, and is
    # deliberately OUTSIDE the mergerfs union — SQLite over FUSE is a locking
    # hazard, and the union is the part that gains and loses drives.
    # storage.nix asserts that separation.
    dataDir = "${stateRoot}/jellyfin";
    cacheDir = "${stateRoot}/jellyfin-cache";
    configDir = "${stateRoot}/jellyfin/config";
  };

  systemd.services.jellyfin = {
    requires = [ "imladris-storage.target" ];
    after = [ "imladris-storage.target" ];
    # See samba-smbd above: a library that half-vanishes is worse than one that
    # is plainly down, because Jellyfin would record the absence as deletions.
    bindsTo = [ "imladris-storage.target" ];
    serviceConfig = {
      # ⚠️ These are enforced only because boot.nix enables the memory cgroup
      # controller; the Raspberry Pi kernel ships it off, and without it
      # MemoryMax is silently ignored.
      #
      # A library scan spikes Jellyfin to 0.8–1.5 GiB on top of a ~200–400 MiB
      # idle footprint. The cap leaves headroom on an 8 GiB board for page cache,
      # which is what actually makes media serving feel fast.
      MemoryMax = "3G";
      MemoryHigh = "2G";
      # Yield to interactive work rather than starving the box during a scan.
      CPUWeight = 50;
      IOWeight = 50;
    };
  };

  systemd.tmpfiles.rules = [
    "d ${stateRoot} 0755 root root -"
    "d ${stateRoot}/jellyfin 0700 jellyfin media -"
    "d ${stateRoot}/jellyfin-cache 0700 jellyfin media -"
  ];

  # ---------------------------------------------------------------------------
  # Exposure
  # ---------------------------------------------------------------------------
  # LAN and tailnet only. No public ingress: Jellyfin's authentication history is
  # not something to put on the internet, and the fleet's Traefik/Authentik edge
  # lives at the other site anyway. Remote access is Tailscale — the Jellyfin
  # clients all accept a custom server URL, so the tailnet name just works.
  networking.firewall = {
    enable = true;
    interfaces.lan0.allowedTCPPorts = [
      445 # SMB
      8096 # Jellyfin HTTP
    ];
    interfaces.lan0.allowedUDPPorts = [ 5353 ]; # mDNS, for Finder discovery
    interfaces.tailscale0.allowedTCPPorts = [
      445
      8096
    ];
  };

  assertions = [
    {
      assertion = !config.services.samba.openFirewall;
      message = "imladris: Samba must not open the firewall on every interface.";
    }
    {
      assertion = !config.services.jellyfin.openFirewall;
      message = "imladris: Jellyfin must not open the firewall on every interface.";
    }
    {
      assertion = !(lib.elem 445 (config.networking.firewall.allowedTCPPorts or [ ]));
      message = "imladris: SMB must never be reachable on all interfaces.";
    }
  ];
}
