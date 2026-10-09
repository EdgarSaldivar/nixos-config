# imladris — Stash, a second organiser over the same archive Jellyfin serves.
#
# Native NixOS service, NOT a container, deliberately. This host runs no
# container runtime, and adding Docker for one Go binary would bring a daemon,
# its own iptables chains, and a second storage root to a Pi whose whole brief
# is to be boring. The nixpkgs module also already isolates the service: a
# dedicated user, a read-only bind of the library, and a long hardening list.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  archiveRoot = "/srv/archive";
  dataDir = "/var/lib/imladris/stash";
  port = 9999;

  # `decord` ships no aarch64 wheels (x86_64 only, and the package is dead);
  # the maintained fork `decord2` provides the same `decord` module.
  # vlm-engine's `decord' requirement is unpinned, so this metadata-only wheel
  # — named 0.6.1 so it outranks PyPI's 0.6.0 — is what pip picks when
  # PIP_FIND_LINKS points at it. Installing it just pulls in decord2, which
  # carries the actual `decord' module, so the plugin is untouched.
  decordShim =
    let
      version = "0.6.1";
    in
    pkgs.runCommand "decord-shim-${version}"
      {
        nativeBuildInputs = [ pkgs.python3 ];
      }
      ''
              mkdir -p $out
              python3 - <<'EOF'
        import base64, hashlib, zipfile

        name, version = "decord", "0.6.1"
        info = f"{name}-{version}.dist-info"

        metadata = "\n".join([
            "Metadata-Version: 2.1",
            f"Name: {name}",
            f"Version: {version}",
            "Summary: aarch64 shim resolving bare decord to the maintained decord2 fork",
            "Requires-Dist: decord2==3.4.0",
            "",
        ])
        wheel = "\n".join([
            "Wheel-Version: 1.0",
            "Generator: nix",
            "Root-Is-Purelib: true",
            "Tag: py3-none-any",
            "",
        ])

        def sha256_url(data: bytes) -> str:
            digest = hashlib.sha256(data).digest()
            return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

        entries = {
            f"{info}/METADATA": metadata.encode(),
            f"{info}/WHEEL": wheel.encode(),
        }
        record = "".join(
            f"{path},{sha256_url(data)},{len(data)}\n" for path, data in entries.items()
        )
        record += f"{info}/RECORD,,\n"
        entries[f"{info}/RECORD"] = record.encode()

        with zipfile.ZipFile(
            f"{name}-{version}-py3-none-any.whl", "w", zipfile.ZIP_DEFLATED
        ) as zf:
            for path, data in entries.items():
                zf.writestr(path, data)
        EOF
              mv decord-0.6.1-py3-none-any.whl $out/
      '';
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

    # CommunityScripts plugins (Haven VLM Connector) run PythonDepManager,
    # which shells out to `git` and `python -m pip` from the service's PATH
    # to install their own dependencies into py_dependencies/. The nixpkgs
    # module only puts ffmpeg, a bare python3 (no pip module) and ruby on the
    # PATH, so every such plugin dies with "git is required but not available"
    # before it even gets to pip. mkBefore puts a pip-carrying python first so
    # `python` resolves to it, ahead of the module's bare python3.
    path = lib.mkBefore [
      pkgs.git
      (pkgs.python3.withPackages (ps: [ ps.pip ]))
    ];

    # The dependencies PythonDepManager pulls from PyPI (numpy, torch, opencv,
    # decord2) are C extensions with no Nix RPATHs, and this host has no
    # /usr/lib and no ld.so.cache, so the dynamic linker cannot find the
    # libstdc++, libz, glib, libGL and X11 libraries those wheels declare as
    # DT_NEEDED. Point the service's linker at the same packages the system
    # already builds. PIP_FIND_LINKS lets pip resolve the local `decord`
    # metapackage wheel (decord itself ships no aarch64 wheels; the fork
    # decord2 provides the same `decord` module) without touching the plugin.
    environment = {
      LD_LIBRARY_PATH = lib.makeLibraryPath [
        # libstdc++.so.6 and libgomp.so.1 live in gcc's `lib' output, not its
        # default (out), which carries only the wrapper scripts.
        pkgs.gcc.passthru.cc.lib
        pkgs.zlib
        pkgs.glib
        pkgs.libglvnd
        pkgs.xorg.libX11
        pkgs.xorg.libXext
        pkgs.xorg.libxcb
        pkgs.libice
        pkgs.libsm
      ];
      # pip resolves the plugin's bare `decord' requirement against this
      # directory first; see decordShim below.
      PIP_FIND_LINKS = "${decordShim}";
    };

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
