# imladris — Stash, a second organiser over the same archive Jellyfin serves.
#
# Native NixOS service, NOT a container, deliberately. This host runs no
# container runtime, and adding Docker for one Go binary would bring a daemon,
# its own iptables chains, and a second storage root to a Pi whose whole brief
# is to be boring. The nixpkgs module also already isolates the service: a
# dedicated user, a read-only bind of the library, and a long hardening list.
{ config, lib, ... }:
let
  archiveRoot = "/srv/archive";
  dataDir = "/var/lib/imladris/stash";
  port = 9999;
in
{
  # In secrets/imladris.yaml with the host's other credentials. The plaintext
  # login is kept there too as `stash_password`, for the operator to read; only
  # the hash is deployed.
  sops.secrets =
    let
      # The module reads these in ExecStartPre, which runs as the service user.
      stashSecret.owner = config.services.stash.user;
    in
    {
      stash_password_hash = stashSecret;
      stash_jwt_secret = stashSecret;
      stash_session_store_key = stashSecret;
    };

  services.stash = {
    enable = true;
    # Same shared group as Jellyfin, so files written over SMB stay readable.
    group = "media";
    openFirewall = false;

    # ⛔ ON NVMe, NOT THE microSD — same reasoning as Jellyfin in ./media.nix.
    # Stash's SQLite database and its generated previews, sprites and
    # transcode cache are the heaviest writers here, and they must stay outside
    # the mergerfs union as well.
    inherit dataDir;

    username = "edgar";
    # ⛔ A BCRYPT HASH, NOT THE PASSWORD. The module copies this file into
    # config.yml verbatim, and Stash checks logins with
    # bcrypt.CompareHashAndPassword against that value. Given the plaintext,
    # every login fails with "invalid credentials" — which is how the first
    # deploy shipped on 2026-10-07.
    passwordFile = config.sops.secrets.stash_password_hash.path;
    jwtSecretKeyFile = config.sops.secrets.stash_jwt_secret.path;
    sessionStoreKeyFile = config.sops.secrets.stash_session_store_key.path;

    # ⚠️ Nix only SEEDS config.yml; it is written only when that file is
    # missing. After first start the Stash UI owns library settings, scrapers
    # and stash-box keys. To make a change here (rotating the password
    # included) take effect, stop stash, delete ${dataDir}/config.yml and start
    # it again. The database is a separate file and is not touched.
    mutableSettings = true;

    # Plugins and scrapers are installed from the Stash UI, so they live in
    # writable directories under dataDir. Left false, the module points both
    # paths at the read-only Nix store and every UI install fails with
    # "read-only file system".
    mutablePlugins = true;
    mutableScrapers = true;

    settings = {
      host = "0.0.0.0";
      inherit port;

      # The module bind-mounts every path listed here READ-ONLY into the
      # service. That is the safety property worth having: Stash has
      # "delete file" actions in its UI, and this archive has exactly one copy
      # (see ./media.nix). With a read-only mount, those actions fail instead of
      # deleting anything.
      stash = [ { path = archiveRoot; } ];

      # One task at a time. This board has four cores and no usable hardware
      # encoder, and it is also serving SMB and Jellyfin.
      parallel_tasks = 1;
    };
  };

  # The module's tmpfiles rule makes the data directory 0755. It holds
  # thumbnails and previews of the library, so lock it down to owner and group.
  systemd.tmpfiles.settings."10-stash-datadir".${dataDir}.d.mode = lib.mkForce "0750";

  systemd.services.stash = {
    # See samba-smbd in ./media.nix. Without these, Stash could start against
    # an empty mountpoint on the microSD and record the whole library as
    # missing.
    requires = [ "imladris-storage.target" ];
    after = [ "imladris-storage.target" ];
    bindsTo = [ "imladris-storage.target" ];
    unitConfig.RequiresMountsFor = [ dataDir ];
    serviceConfig = {
      # Enforced only because boot.nix enables the memory cgroup controller.
      # Stash idles around 200 MiB; the headroom is for ffmpeg during generate
      # tasks and live transcodes.
      MemoryMax = "2G";
      MemoryHigh = "1536M";
      # Yield to SMB and Jellyfin, as Jellyfin does.
      CPUWeight = 50;
      IOWeight = 50;
    };
  };

  # LAN and tailnet only, matching Jellyfin. No public ingress.
  networking.firewall.interfaces = {
    lan0.allowedTCPPorts = [ port ];
    tailscale0.allowedTCPPorts = [ port ];
  };

  assertions = [
    {
      assertion = !config.services.stash.openFirewall;
      message = "imladris: Stash must not open the firewall on every interface.";
    }
    {
      assertion = !lib.elem port (config.networking.firewall.allowedTCPPorts or [ ]);
      message = "imladris: Stash must never be reachable on all interfaces.";
    }
  ];
}
