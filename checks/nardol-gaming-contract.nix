{
  lib,
  pkgs,
  nixosConfigurations,
  darwinConfigurations,
  ...
}:

# Headless gaming deliberately avoids a display manager/DE while
# keeping the exact GPU, input, persistent-state, and container
# contracts Wolf needs. Catch a future "cleanup" that silently puts
# Docker back on root or turns the pinned container privileged again.
let
  cfg = nixosConfigurations.nardol.config;
  nardolPkgs = nixosConfigurations.nardol.pkgs;
  nvidia = cfg.hardware.nvidia;
  wolf = cfg.virtualisation.oci-containers.containers.wolf;
  expectedWolfImage = "ghcr.io/games-on-whales/wolf@sha256:ff82c125c9b79b2e9443de2b0eaec40c904edb03291680d408cccd57c1d59c76";
  expectedPulseImage = "ghcr.io/games-on-whales/pulseaudio@sha256:5f05a7102bdb6c464a96cb33770eb10c7fb6ca0c007961e3edd5915907643bed";
  expectedEglVendorFiles = "/run/opengl-driver/share/glvnd/egl_vendor.d/10_nvidia.json:/usr/share/glvnd/egl_vendor.d/50_mesa.json";
  expectedNvrtcContainerPath = "/opt/nardol-nvrtc";
  expectedNvrtcMount = "${nardolPkgs.cudaPackages.cuda_nvrtc.lib}/lib:${expectedNvrtcContainerPath}:ro";
  expectedVbanClientAddress = "10.0.0.17";
  expectedVbanPort = 6980;
  expectedVbanStream = "NardolMic";
  expectedMicSource = "nardol_client_mic";
  expectedMicMount = "/etc/nardol/wolf-client-mic.sh:/etc/cont-init.d/95-nardol-client-mic.sh:ro";
  expectedVbanFirewallSource = "-s ${expectedVbanClientAddress}/32";
  expectedVbanFirewallPort = "--dport ${toString expectedVbanPort}";
  vbanService = cfg.systemd.services."nardol-vban-microphone";
  vbanPackageText = builtins.readFile ../hosts/nixos/nardol/vban.nix;
  micInit = cfg.environment.etc."nardol/wolf-client-mic.sh";
  wolfPaths = import ../hosts/nixos/nardol/wolf-paths.nix;
  imagePins = {
    "ghcr.io/games-on-whales/es-de:edge" =
      "ghcr.io/games-on-whales/es-de@sha256:f5d1037e9dd6ff7406e190e00457152d0a9dcb4adbc32fe2132585cb5bbe7829";
    "ghcr.io/games-on-whales/firefox:edge" =
      "ghcr.io/games-on-whales/firefox@sha256:1ea7331934d31d346079fb67462b371586d65b5ebb792acee8c0e64e87c185b1";
    "ghcr.io/games-on-whales/kodi:edge" =
      "ghcr.io/games-on-whales/kodi@sha256:e3db2ca9492b85f98c253436c22d47c841009d278b7e8dc7f3f349aca2ebfe8a";
    "ghcr.io/games-on-whales/lutris:edge" =
      "ghcr.io/games-on-whales/lutris@sha256:207005d9e1a839814c7c2b91fa25190d40c388c7dc004eec593556bd807f99f2";
    "ghcr.io/games-on-whales/pegasus:edge" =
      "ghcr.io/games-on-whales/pegasus@sha256:29e7ab082f1c73a92ff25dff66983a83790d109ea49e0826cb0279b7fe5eacd8";
    "ghcr.io/games-on-whales/prismlauncher:edge" =
      "ghcr.io/games-on-whales/prismlauncher@sha256:e2c610f666b019a2e31482641cab6c3330a24add41fd88f939a15f327bf9dda0";
    "ghcr.io/games-on-whales/retroarch:edge" =
      "ghcr.io/games-on-whales/retroarch@sha256:bbcf4523e589fc7177b522ce56ba9507c6530caaf1999e37b37062a189f18cf2";
    "ghcr.io/games-on-whales/wolf-ui:main" =
      "ghcr.io/games-on-whales/wolf-ui@sha256:cd6de1158b29068e4a4d4ce6312976067517239be97200a391be758a6ddfcf9b";
    "ghcr.io/games-on-whales/xfce:edge" =
      "ghcr.io/games-on-whales/xfce@sha256:2ce1db7432bcb60caf5b3da23ea0ad5a24f300f3e7f346045fd6ba74a477ebcd";
    "ghcr.io/edgarsaldivar/nardol-steam-tools:git-214fce8091fc0524d64996a3b225ee3a98251c36" =
      "ghcr.io/edgarsaldivar/nardol-steam-tools@sha256:629951ab9461def4aa78424d45a5748c7a114b421a46c68a86609126cb1238d8";
  };
  imageTags = builtins.attrNames imagePins;
  wolfConfigText = builtins.replaceStrings imageTags (map (tag: imagePins.${tag}) imageTags) (
    builtins.readFile ../hosts/nixos/nardol/wolf-config.template.toml
  );
  wolfConfig = builtins.fromTOML wolfConfigText;
  steamApps = lib.concatMap (
    profile: builtins.filter (app: (app.title or null) == "Steam") (profile.apps or [ ])
  ) wolfConfig.profiles;
  validateWolfConfig = import ../hosts/nixos/nardol/wolf-validator.nix {
    inherit lib;
    paths = wolfPaths;
  };
  mapProfile =
    profileId: operation: configValue:
    configValue
    // {
      profiles = map (
        profile: if profile.id == profileId then operation profile else profile
      ) configValue.profiles;
    };
  mapApp =
    profileId: title: operation:
    mapProfile profileId (
      profile:
      profile
      // {
        apps = map (app: if app.title == title then operation app else app) profile.apps;
      }
    );
  addMount =
    mount: app:
    app
    // {
      runner = app.runner // {
        mounts = app.runner.mounts ++ [ mount ];
      };
    };
  malformedFixtures = [
    (wolfConfig // { profiles = wolfConfig.profiles ++ [ (builtins.head wolfConfig.profiles) ]; })
    (mapProfile "guest" (
      profile: profile // { apps = profile.apps ++ [ (builtins.head profile.apps) ]; }
    ) wolfConfig)
    (mapApp "guest" "Steam" (
      app:
      app
      // {
        runner = app.runner // {
          mounts = map (
            mount: builtins.replaceStrings [ wolfPaths.guest.steamapps ] [ wolfPaths.user.steamapps ] mount
          ) app.runner.mounts;
        };
      }
    ) wolfConfig)
    (mapApp "guest" "Steam" (addMount wolfPaths.user.mounts.mods) wolfConfig)
    (mapApp "guest" "Steam" (addMount "guest-data:/guest:rw") wolfConfig)
    (mapApp "guest" "Steam" (addMount "lutris:/var/lutris/:rw") wolfConfig)
    (mapApp "moonlight-profile-id" "Test ball" (
      app:
      app
      // {
        runner = app.runner // {
          run_cmd = "sh -c arbitrary-root-command";
        };
      }
    ) wolfConfig)
    (mapApp "guest" "Steam" (
      app:
      app
      // {
        runner = app.runner // {
          type = "process";
        };
      }
    ) wolfConfig)
    (mapApp "guest" "Steam" (app: app // { start_virtual_compositor = false; }) wolfConfig)
  ];
  expectedGamingDirectories = [
    "d ${wolfPaths.user.steamapps} 0750 1000 1000 - -"
    "d ${wolfPaths.user.nonsteam} 0750 1000 1000 - -"
    "d ${wolfPaths.user.modStaging} 0750 1000 1000 - -"
    "d ${wolfPaths.user.mods} 0750 1000 1000 - -"
    "d ${wolfPaths.user.backups} 0750 1000 1000 - -"
    "d ${wolfPaths.user.downloads} 0750 1000 1000 - -"
    "d ${wolfPaths.user.logs} 0750 1000 1000 - -"
    "d ${wolfPaths.user.manifests} 0750 1000 1000 - -"
    "d ${wolfPaths.user.tools} 0750 1000 1000 - -"
    "d ${wolfPaths.guest.steamapps} 0750 1000 1000 - -"
    "d ${wolfPaths.guest.nonsteam} 0750 1000 1000 - -"
    "d ${wolfPaths.guest.modStaging} 0750 1000 1000 - -"
    "d ${wolfPaths.guest.mods} 0750 1000 1000 - -"
    "d ${wolfPaths.guest.backups} 0750 1000 1000 - -"
    "d ${wolfPaths.guest.downloads} 0750 1000 1000 - -"
    "d ${wolfPaths.guest.logs} 0750 1000 1000 - -"
    "d ${wolfPaths.guest.manifests} 0750 1000 1000 - -"
    "d ${wolfPaths.guest.tools} 0750 1000 1000 - -"
  ];
  wolfPreStart = cfg.systemd.services.docker-wolf.serviceConfig.ExecStartPre or [ ];
  wakeLink = cfg.systemd.network.links."10-nardol-i211-wake";
in
if cfg.services.xserver.enable then
  throw "nardol must remain headless; the NVIDIA selector must not enable X11"
else if cfg.programs.steam.enable || !cfg.hardware.steam-hardware.enable then
  throw "nardol must keep only Steam hardware rules; the client belongs inside Wolf"
else if
  !nvidia.open
  || !nvidia.modesetting.enable
  || !nvidia.nvidiaPersistenced
  || nvidia.nvidiaSettings
  || nvidia.package.outPath != cfg.boot.kernelPackages.nvidiaPackages.production.outPath
then
  throw "nardol NVIDIA must use the headless open-module production-driver contract"
else if
  !cfg.hardware.nvidia-container-toolkit.enable
  || cfg.virtualisation.docker.enableNvidia
  || cfg.virtualisation.docker.daemon.settings.data-root != "/srv/docker"
  || !(cfg.virtualisation.docker.daemon.settings.runtimes ? nvidia)
  || !lib.elem "/srv/docker" cfg.systemd.services.docker.unitConfig.RequiresMountsFor
then
  throw "nardol Docker GPU/runtime or SN850X data-root contract changed"
else if
  wolf.image != expectedWolfImage
  || wolf.pull != "missing"
  || wolf.privileged
  || wolf.networks != [ "host" ]
  || wolf.environment.__EGL_VENDOR_LIBRARY_FILENAMES != expectedEglVendorFiles
  || wolf.environment.HOST_APPS_STATE_FOLDER != "/var/lib/wolf"
  || wolf.environment.LD_LIBRARY_PATH != expectedNvrtcContainerPath
  || wolf.environment.WOLF_PULSE_IMAGE != expectedPulseImage
  || wolf.environment.WOLF_USE_ZERO_COPY != "FALSE"
  || !lib.any (lib.hasInfix "nardol-wolf-config-policy") wolfPreStart
  || !lib.elem "/srv/wolf/data:/var/lib/wolf:rw" wolf.volumes
  || lib.elem "/srv/wolf/config:/etc/wolf:rw" wolf.volumes
  || !lib.elem expectedNvrtcMount wolf.volumes
  || !validateWolfConfig wolfConfig
  || !lib.all (fixture: !(validateWolfConfig fixture)) malformedFixtures
  || !lib.elem "/dev/uinput:/dev/uinput" wolf.devices
  || !lib.elem "/dev/uhid:/dev/uhid" wolf.devices
  || !lib.all (rule: lib.elem rule cfg.systemd.tmpfiles.rules) expectedGamingDirectories
then
  throw "nardol Wolf image policy, privilege, state, network, NVIDIA EGL/NVRTC/copy-path, or input contract changed"
else if
  builtins.length steamApps != 2
  || !lib.all (app: lib.elem expectedMicMount app.runner.mounts) steamApps
  ||
    micInit.text != ''
      # Sourced by the Games on Whales entrypoint before it starts Steam.
      export PULSE_SOURCE=${expectedMicSource}
    ''
  || micInit.mode != "0444"
  || !lib.elem "multi-user.target" vbanService.wantedBy
  || !lib.elem "docker-wolf.service" vbanService.requires
  || !lib.elem "docker-wolf.service" vbanService.after
  || !lib.elem "docker-wolf.service" vbanService.partOf
  || vbanService.serviceConfig.Restart != "on-failure"
  || !vbanService.serviceConfig.DynamicUser
  || !lib.hasInfix "nardol-vban-mic-prepare" vbanService.preStart
  || !lib.hasInfix "nardol-vban-mic-cleanup" vbanService.postStop
  || !lib.hasInfix "--ipaddress=${expectedVbanClientAddress}" vbanService.script
  || !lib.hasInfix "--port=${toString expectedVbanPort}" vbanService.script
  || !lib.hasInfix "--streamname=${expectedVbanStream}" vbanService.script
  || !lib.hasInfix "--backend=pulseaudio" vbanService.script
  || !lib.hasInfix "--device=nardol_client_mic_sink" vbanService.script
  || !lib.hasInfix "iptables -w -A nixos-fw" cfg.networking.firewall.extraCommands
  || !lib.hasInfix "-i eth0" cfg.networking.firewall.extraCommands
  || !lib.hasInfix expectedVbanFirewallSource cfg.networking.firewall.extraCommands
  || !lib.hasInfix expectedVbanFirewallPort cfg.networking.firewall.extraCommands
  || !lib.hasInfix "-j nixos-fw-accept" cfg.networking.firewall.extraCommands
  || !lib.hasInfix "iptables -w -D nixos-fw" cfg.networking.firewall.extraStopCommands
  || !lib.hasInfix expectedVbanFirewallSource cfg.networking.firewall.extraStopCommands
  || !lib.hasInfix expectedVbanFirewallPort cfg.networking.firewall.extraStopCommands
  || lib.elem expectedVbanPort cfg.networking.firewall.allowedUDPPorts
  || cfg.networking.nftables.enable
  || !lib.hasInfix ''rev = "v''${finalAttrs.version}";'' vbanPackageText
  || !lib.hasInfix "sha256-Zt+n2ESKH2Q10kS7GyKGfDEMfVkAQDzvjhseTO/dbxs=" vbanPackageText
then
  throw "nardol VBAN microphone receiver, Pulse source, Steam mount, pin, or source-scoped firewall contract changed"
else if
  !cfg.hardware.uinput.enable
  || !lib.elem "uhid" cfg.boot.kernelModules
  || cfg.users.users.edgar.uid != 1000
  || cfg.users.groups.edgar.gid != 1000
  || wakeLink.matchConfig.MACAddress != "9c:6b:00:36:e0:e8"
  || wakeLink.linkConfig.WakeOnLan != "magic"
  || !lib.elem nardolPkgs.ethtool cfg.environment.systemPackages
then
  throw "nardol Wolf input, persistent UID/GID, or headless wake contract changed"
else
  pkgs.runCommand "nardol-gaming-contract-ok" { } "touch $out"
