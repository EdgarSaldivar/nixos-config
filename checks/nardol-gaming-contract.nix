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
  # nardol pins NVIDIA production maintenance 595.99.02 (later than nixpkgs'
  # 595.71.05 production branch) for the Crimson Desert hang fix landed in
  # 595.84. The role source owns the pin; this contract only asserts the driver,
  # persistenced, and kernel-specific open modules all use that driver version.
  expectedNvidiaVersion = "595.99.02";
  wolf = cfg.virtualisation.oci-containers.containers.wolf;
  expectedWolfImage = "ghcr.io/games-on-whales/wolf@sha256:ff82c125c9b79b2e9443de2b0eaec40c904edb03291680d408cccd57c1d59c76";
  expectedPulseImage = "ghcr.io/games-on-whales/pulseaudio@sha256:5f05a7102bdb6c464a96cb33770eb10c7fb6ca0c007961e3edd5915907643bed";
  expectedEglVendorFiles = "/run/opengl-driver/share/glvnd/egl_vendor.d/10_nvidia.json:/usr/share/glvnd/egl_vendor.d/50_mesa.json";
  expectedNvrtcContainerPath = "/opt/nardol-nvrtc";
  expectedNvrtcMount = "${nardolPkgs.cudaPackages.cuda_nvrtc.lib}/lib:${expectedNvrtcContainerPath}:ro";
  expectedVbanClientAddress = "10.0.0.17";
  expectedVbanPort = 6980;
  expectedVbanStream = "Talkie";
  expectedMicSource = "nardol_client_mic";
  expectedMicMount = "/etc/nardol/wolf-client-mic.sh:/etc/cont-init.d/95-nardol-client-mic.sh:ro";
  expectedVbanFirewallSource = "-s ${expectedVbanClientAddress}/32";
  expectedVbanFirewallPort = "--dport ${toString expectedVbanPort}";
  vbanService = cfg.systemd.services."nardol-vban-microphone";
  vbanPackageText = builtins.readFile ../hosts/nixos/nardol/vban.nix;
  micInit = cfg.environment.etc."nardol/wolf-client-mic.sh";
  expectedGameFocusMount = "/etc/nardol/sway-game-focus.conf:/etc/sway/config.d/60-nardol-game-focus.conf:ro";
  gameFocusRule = cfg.environment.etc."nardol/sway-game-focus.conf";
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
  wakeLink = cfg.systemd.network.links."10-nardol-rtl8125-wake";

  # ⛔ The readiness check LATCHES, so something must re-run it after resume.
  #
  # nardol-gaming-readiness is RemainAfterExit=true — required, because
  # docker-wolf Requires= it and that dependency only holds while it reports
  # active. The cost is that it validates once at boot and never again, while
  # every assertion it makes (render node binding, nvidia_drm modeset, the CDI
  # spec, nvidia-smi reachability) is invalidated by a sleep transition.
  #
  # Without the resume unit a bad resume leaves the unit active, Wolf answering
  # its port, and the GPU unable to encode, with nothing reporting a fault. Pin
  # the whole wiring rather than the unit's existence: an ExecStart that no
  # longer restarts the readiness service, or a WantedBy that no longer covers
  # the sleep targets, is the same silent failure wearing the unit's name.
  resumeUnit = cfg.systemd.services.nardol-gaming-readiness-resume or null;
  sleepTargets = [
    "suspend.target"
    "hibernate.target"
    "hybrid-sleep.target"
    "suspend-then-hibernate.target"
  ];
  # ⛔ The idle-suspend loop must stay serialised and must not burst after resume.
  #
  # Two failure shapes are cheap to pin structurally. Without ExecCondition
  # flock, a second timer firing while the first run is deciding lets both see a
  # stale counter and double-count toward the threshold. With Persistent=true,
  # systemd fires catch-up runs for every poll missed while asleep — immediately
  # after a resume, which is exactly when the host is least likely to be idle.
  #
  # What is NOT pinned here, and cannot be structurally: that the check fails
  # closed. That property lives in the script and is covered by review and by the
  # comments in hosts/nixos/nardol/idle-suspend.nix.
  idleTimer = cfg.systemd.timers.nardol-idle-suspend or null;
  idleService = cfg.systemd.services.nardol-idle-suspend or null;
  # Idle auto-suspend is currently disabled (see hosts/nixos/nardol/default.nix);
  # when it is re-enabled, the invariants below apply again.
  idleBroken =
    (idleTimer != null || idleService != null)
    && (
      idleTimer == null
      || idleService == null
      || !lib.elem "timers.target" (idleTimer.wantedBy or [ ])
      || (idleTimer.timerConfig.OnUnitActiveSec or null) == null
      || (idleTimer.timerConfig.Persistent or false)
      || !lib.hasInfix "flock" (idleService.serviceConfig.ExecCondition or "")
    );

  resumeExec = if resumeUnit == null then "" else (resumeUnit.serviceConfig.ExecStart or "");
  verifyUnit = cfg.systemd.services.nardol-gaming-verify or null;
  resumeUnitBroken =
    resumeUnit == null
    || verifyUnit == null
    || !lib.all (t: lib.elem t (resumeUnit.wantedBy or [ ])) sleepTargets
    || !lib.all (t: lib.elem t (resumeUnit.after or [ ])) sleepTargets
    # It must drive the standalone verifier...
    || !lib.hasInfix "nardol-gaming-verify" resumeExec
    # ...and must NOT restart the gate. docker-wolf Requires= the gate and
    # systemd propagates a stop across Requires, so restarting it tears Wolf
    # down — wasteful when idle, and it would kill a live session. This exact
    # mistake shipped on 2026-09-12; Wolf's PID moved 27205 -> 27831 the moment
    # the gate deactivated. Forbidden by name, not merely replaced.
    || lib.hasInfix "restart nardol-gaming-readiness" resumeExec
    # The verifier must fail closed, or a host that cannot encode keeps serving.
    || !lib.elem "nardol-gaming-halt-wolf.service" (verifyUnit.onFailure or [ ])
    # Nothing may depend on the verifier, or running it inherits the same
    # stop-propagation problem it exists to avoid.
    || lib.any (
      svc: lib.elem "nardol-gaming-verify.service" ((svc.requires or [ ]) ++ (svc.requisite or [ ]))
    ) (lib.attrValues cfg.systemd.services)
    || !cfg.systemd.services.nardol-gaming-readiness.serviceConfig.RemainAfterExit;
in
if idleBroken then
  throw "nardol's idle-suspend timer must be wantedBy timers.target, poll on OnUnitActiveSec, keep Persistent=false so a resume does not trigger catch-up runs, and serialise with flock"
else if resumeUnitBroken then
  throw "nardol-gaming-readiness latches (RemainAfterExit); nardol-gaming-readiness-resume must restart it on every sleep target, or a bad resume leaves the host reporting ready while unable to encode"
else if cfg.services.xserver.enable then
  throw "nardol must remain headless; the NVIDIA selector must not enable X11"
else if cfg.programs.steam.enable || !cfg.hardware.steam-hardware.enable then
  throw "nardol must keep only Steam hardware rules; the client belongs inside Wolf"
else if
  !nvidia.open
  || !nvidia.modesetting.enable
  || !nvidia.nvidiaPersistenced
  # ⛔ Suspend safety. powerManagement.enable is what installs
  # nvidia-suspend/resume/hibernate; without those units nothing drives
  # /proc/driver/nvidia/suspend and GPU state is not saved across S3. The
  # resulting failure is silent — nardol-gaming-readiness is
  # RemainAfterExit=true so it keeps reporting active from its boot-time run,
  # and Wolf keeps answering its port, on a host that cannot encode a frame.
  # Measured off on the live host 2026-09-12 while /proc/driver/nvidia/suspend
  # existed and went unused. finegrained must stay off: it is Optimus laptop
  # runtime-D3 and conflicts with nvidiaPersistenced.
  || !nvidia.powerManagement.enable
  || nvidia.powerManagement.finegrained
  || nvidia.nvidiaSettings
  || nvidia.package.version != expectedNvidiaVersion
  ||
    nvidia.package.open.version != "${expectedNvidiaVersion}-${cfg.boot.kernelPackages.kernel.version}"
  || nvidia.package.persistenced.version != expectedNvidiaVersion
then
  throw "nardol NVIDIA must use the headless open-module production-driver contract pinned to ${expectedNvidiaVersion}"
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
  # ⛔ The firewall rule above matches `-i eth0`, so the interface NAME is part of
  # this contract -- assert it is DECLARED rather than inherited.
  #
  # nardol has predictable naming enabled (no net.ifnames=0) and udev computes
  # enp9s0 for this card. It is eth0 only because a matching .link file with no
  # NamePolicy suppresses renaming. Without pinning Name here, adding a NamePolicy
  # to that link -- or deleting it -- renames the card and the VBAN rule stops
  # matching, silently: the microphone simply stops working, with no error anywhere.
  || (cfg.systemd.network.links."10-nardol-rtl8125-wake".linkConfig.Name or null) != "eth0"
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
  !lib.all (app: lib.elem expectedGameFocusMount app.runner.mounts) steamApps
  || gameFocusRule.source != ../hosts/nixos/nardol/wolf/sway-game-focus.conf
  || gameFocusRule.mode != "0444"
then
  # Without this rule native Linux games launch hidden behind fullscreen Big
  # Picture: audio plays but the stream shows only the Steam launcher.
  throw "nardol Steam sessions must mount the host-owned sway game-focus rule"
else if
  !cfg.hardware.uinput.enable
  || !lib.elem "uhid" cfg.boot.kernelModules
  || cfg.users.users.edgar.uid != 1000
  || cfg.users.groups.edgar.gid != 1000
  || wakeLink.matchConfig.MACAddress != "1c:86:0b:3f:08:53"
  || wakeLink.linkConfig.WakeOnLan != "magic"
  || !lib.elem nardolPkgs.ethtool cfg.environment.systemPackages
then
  throw "nardol Wolf input, persistent UID/GID, or headless wake contract changed"
else
  pkgs.runCommand "nardol-gaming-contract-ok" { } "touch $out"
