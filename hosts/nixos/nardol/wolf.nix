# Headless Games on Whales / Wolf host.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  # Multi-architecture GoW manifests resolved on 2026-08-07; the Nardol Steam
  # toolbox was built, scanned, and attested on 2026-08-08. Update the
  # controller, template, and child pins together, then run a Moonlight smoke
  # test instead of letting any of these root-equivalent containers drift.
  wolfImage = "ghcr.io/games-on-whales/wolf@sha256:ff82c125c9b79b2e9443de2b0eaec40c904edb03291680d408cccd57c1d59c76";
  steamToolsTag = "ghcr.io/edgarsaldivar/nardol-steam-tools:git-214fce8091fc0524d64996a3b225ee3a98251c36";
  steamToolsImage = "ghcr.io/edgarsaldivar/nardol-steam-tools@sha256:629951ab9461def4aa78424d45a5748c7a114b421a46c68a86609126cb1238d8";
  upstreamSteamImage = "ghcr.io/games-on-whales/steam@sha256:ded0b1b47acd9adb8af9f068342f26ac31008904d9bbb91045d1a04e7d66a632";
  wolfImagePins = {
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
    "ghcr.io/games-on-whales/pulseaudio:master" =
      "ghcr.io/games-on-whales/pulseaudio@sha256:5f05a7102bdb6c464a96cb33770eb10c7fb6ca0c007961e3edd5915907643bed";
    "ghcr.io/games-on-whales/retroarch:edge" =
      "ghcr.io/games-on-whales/retroarch@sha256:bbcf4523e589fc7177b522ce56ba9507c6530caaf1999e37b37062a189f18cf2";
    "${steamToolsTag}" = steamToolsImage;
    "ghcr.io/games-on-whales/wolf-ui:main" =
      "ghcr.io/games-on-whales/wolf-ui@sha256:cd6de1158b29068e4a4d4ce6312976067517239be97200a391be758a6ddfcf9b";
    "ghcr.io/games-on-whales/xfce:edge" =
      "ghcr.io/games-on-whales/xfce@sha256:2ce1db7432bcb60caf5b3da23ea0ad5a24f300f3e7f346045fd6ba74a477ebcd";
  };
  # Normalise a generated pre-toolbox config without rewriting unrelated app
  # state. These sources are migration inputs, not valid template identities.
  wolfImageMigrations = {
    "ghcr.io/games-on-whales/steam:edge" = steamToolsImage;
    "${upstreamSteamImage}" = steamToolsImage;
  };
  wolfPaths = import ./wolf-paths.nix;
  wolfImageTags = builtins.attrNames wolfImagePins;
  wolfConfigText = builtins.replaceStrings wolfImageTags (map (
    tag: wolfImagePins.${tag}
  ) wolfImageTags) (builtins.readFile ./wolf-config.template.toml);
  wolfConfigImageLines = builtins.filter (
    line: builtins.match "[[:space:]]*image[[:space:]]*=.*" line != null
  ) (lib.splitString "\n" wolfConfigText);
  wolfConfigData = builtins.fromTOML wolfConfigText;
  validateWolfConfig = import ./wolf-validator.nix {
    inherit lib;
    paths = wolfPaths;
  };
  wolfConfigTemplate = pkgs.writeText "nardol-wolf-config.toml" wolfConfigText;
  wolfImageRewrites = wolfImagePins // wolfImageMigrations;
  wolfPathsJson = pkgs.writeText "nardol-wolf-paths.json" (builtins.toJSON wolfPaths);
  wolfImageRewritesJson = pkgs.writeText "nardol-wolf-image-rewrites.json" (
    builtins.toJSON wolfImageRewrites
  );
  wolfState = "/srv/wolf";
  wolfHostStatePath = "${wolfState}/data";
  wolfContainerStatePath = "/var/lib/wolf";
  renderNode = "/dev/dri/renderD128";
  nvidiaEglVendorFile = "/run/opengl-driver/share/glvnd/egl_vendor.d/10_nvidia.json";
  wolfEglVendorFiles = "${nvidiaEglVendorFile}:/usr/share/glvnd/egl_vendor.d/50_mesa.json";
  nvidiaAllocatorHostPath = "/run/opengl-driver/lib/libnvidia-allocator.so.1";
  nvrtcLib = pkgs.cudaPackages.cuda_nvrtc.lib;
  nvrtcContainerPath = "/opt/nardol-nvrtc";
  nvidiaSmi = lib.getExe' config.hardware.nvidia.package "nvidia-smi";
  nvidiaToolkit = config.hardware.nvidia-container-toolkit.package;
  nvidiaToolkitTools = lib.getOutput "tools" nvidiaToolkit;
  nvidiaRuntime = lib.getExe' nvidiaToolkitTools "nvidia-container-runtime";
  docker = lib.getExe config.virtualisation.docker.package;
  vban = pkgs.callPackage ./vban.nix { };
  vbanClientAddress = "10.0.0.17";
  vbanPort = 6980;
  vbanStreamName = "NardolMic";
  wolfPulseSocket = "/run/wolf/pulse-socket";
  wolfMicSink = "nardol_client_mic_sink";
  wolfMicSource = "nardol_client_mic";
  wolfPython = pkgs.python3.withPackages (pythonPackages: [ pythonPackages.tomlkit ]);
  wolfConfigPolicy = pkgs.writeShellApplication {
    name = "nardol-wolf-config-policy";
    text = ''
      exec ${wolfPython}/bin/python ${./wolf-reconcile.py} \
        --template ${wolfConfigTemplate} \
        --config ${wolfHostStatePath}/cfg/config.toml \
        --paths-json ${wolfPathsJson} \
        --rewrites-json ${wolfImageRewritesJson}
    '';
  };
  vbanMicPrepare = pkgs.writeShellApplication {
    name = "nardol-vban-mic-prepare";
    runtimeInputs = [
      pkgs.coreutils
      pkgs.gnugrep
      pkgs.pulseaudio
    ];
    text = ''
      export PULSE_SERVER=unix:${wolfPulseSocket}

      ready=
      for _ in $(seq 1 60); do
        if test -S ${wolfPulseSocket} && pactl info >/dev/null 2>&1; then
          ready=1
          break
        fi
        sleep 1
      done
      test -n "$ready"

      if ! pactl list short sinks | cut -f2 | grep -Fxq ${wolfMicSink}; then
        pactl load-module module-null-sink \
          sink_name=${wolfMicSink} \
          rate=48000 \
          channels=1 \
          channel_map=mono \
          sink_properties=device.description=Nardol-Client-Microphone-Receiver \
          >/dev/null
      fi

      if ! pactl list short sources | cut -f2 | grep -Fxq ${wolfMicSource}; then
        pactl load-module module-remap-source \
          master=${wolfMicSink}.monitor \
          source_name=${wolfMicSource} \
          channels=1 \
          channel_map=mono \
          source_properties=device.description=Nardol-Client-Microphone \
          >/dev/null
      fi
    '';
  };
  vbanMicCleanup = pkgs.writeShellApplication {
    name = "nardol-vban-mic-cleanup";
    runtimeInputs = [
      pkgs.gawk
      pkgs.pulseaudio
    ];
    text = ''
      export PULSE_SERVER=unix:${wolfPulseSocket}
      if ! test -S ${wolfPulseSocket} || ! pactl info >/dev/null 2>&1; then
        exit 0
      fi

      remap_id="$(pactl list short modules | awk \
        '$2 == "module-remap-source" && $0 ~ /source_name=${wolfMicSource}([[:space:]]|$)/ { print $1; exit }')"
      if test -n "$remap_id"; then
        pactl unload-module "$remap_id" || true
      fi

      sink_id="$(pactl list short modules | awk \
        '$2 == "module-null-sink" && $0 ~ /sink_name=${wolfMicSink}([[:space:]]|$)/ { print $1; exit }')"
      if test -n "$sink_id"; then
        pactl unload-module "$sink_id" || true
      fi
    '';
  };
in
{
  # Wolf stable still adds `Runtime = "nvidia"` to the child app containers it
  # creates. Supply that narrow alias in CDI mode without enabling NixOS's
  # deprecated global-default compatibility option for unrelated containers.
  virtualisation.docker = {
    daemon.settings.runtimes.nvidia = {
      path = nvidiaRuntime;
      args = [ ];
    };
    extraPackages = [ nvidiaToolkitTools ];
  };

  environment.etc."nvidia-container-runtime/config.toml".text = ''
    disable-require = true
    supported-driver-capabilities = "compat32,compute,display,graphics,ngx,utility,video"

    [nvidia-container-cli]
    environment = []
    ldconfig = "@${lib.getExe' pkgs.glibc "ldconfig"}"
    load-kmods = true
    no-cgroups = false
    path = "${lib.getExe' pkgs.libnvidia-container "nvidia-container-cli"}"

    [nvidia-container-runtime]
    mode = "cdi"
    runtimes = ["docker-runc", "runc", "crun"]

    [nvidia-container-runtime-hook]
    path = "${lib.getExe' nvidiaToolkitTools "nvidia-container-runtime-hook"}"
    skip-mode-detection = false

    [nvidia-ctk]
    path = "${lib.getExe' nvidiaToolkit "nvidia-ctk"}"
  '';

  environment.etc."nardol/wolf-client-mic.sh" = {
    text = ''
      # Sourced by the Games on Whales entrypoint before it starts Steam.
      export PULSE_SOURCE=${wolfMicSource}
    '';
    mode = "0444";
  };

  virtualisation.oci-containers = {
    backend = "docker";
    containers.wolf = {
      image = wolfImage;
      hostname = "wolf";
      pull = "missing";
      privileged = false;
      user = "0:0";
      networks = [ "host" ];
      environment = {
        # NixOS exposes NVIDIA's GLVND registration below /run, while the
        # Ubuntu-based Wolf image otherwise finds only Mesa below /usr/share.
        # Keep NVIDIA first for the selected DRM node, but retain Mesa because
        # this GLVND combination advertises EGL_EXT_device_enumeration only
        # when both manifests are loaded. Wolf's compositor requires it.
        __EGL_VENDOR_LIBRARY_FILENAMES = wolfEglVendorFiles;
        HOST_APPS_STATE_FOLDER = wolfContainerStatePath;
        # cudaconvertscale is registered only when GStreamer's nvcodec plugin
        # can load NVRTC. The NVIDIA container runtime supplies driver
        # libraries, but NVRTC is a CUDA redistributable rather than a driver.
        LD_LIBRARY_PATH = nvrtcContainerPath;
        NVIDIA_DRIVER_CAPABILITIES = "all";
        NVIDIA_VISIBLE_DEVICES = "all";
        WOLF_DEFAULT_RUN_GID = "1000";
        WOLF_DEFAULT_RUN_UID = "1000";
        WOLF_PULSE_IMAGE = wolfImagePins."ghcr.io/games-on-whales/pulseaudio:master";
        WOLF_RENDER_NODE = renderNode;
        # Wolf's NVIDIA zero-copy path currently fails while allocating its
        # GsCUDABuf DMA buffer on this NixOS/NVIDIA stack. Keep NVENC enabled,
        # but use the supported CUDA upload/copy path instead.
        WOLF_USE_ZERO_COPY = "FALSE";
        WOLF_STOP_CONTAINER_ON_EXIT = "TRUE";
        XDG_RUNTIME_DIR = "/run/wolf";
      };
      volumes = [
        "${wolfHostStatePath}:${wolfContainerStatePath}:rw"
        "/run/wolf:/run/wolf:rw"
        "/var/run/docker.sock:/var/run/docker.sock:rw"
        "/dev:/dev:rw"
        "/run/udev:/run/udev:rw"
        "${nvrtcLib}/lib:${nvrtcContainerPath}:ro"
      ];
      devices = [
        "/dev/dri:/dev/dri"
        "/dev/uinput:/dev/uinput"
        "/dev/uhid:/dev/uhid"
      ];
      extraOptions = [
        "--device-cgroup-rule=c 13:* rmw"
        "--ipc=host"
        "--runtime=nvidia"
      ];
    };
  };

  # Wolf derives both cfg/ (configuration, certificate, and pairing) and
  # profile-data/ from HOST_APPS_STATE_FOLDER, so keep that combined state on
  # the encrypted SSD and separate only the large shared game library.
  systemd.tmpfiles.rules = [
    "d /srv/docker 0710 root docker - -"
    "d /srv/games 0750 1000 1000 - -"
    "d ${wolfPaths.user.nonsteam} 0750 1000 1000 - -"
    "d ${wolfPaths.user.steamapps} 0750 1000 1000 - -"
    # Hardlink/atomic-rename deployers need staging inside the same container
    # mount as their game targets, not merely on the same host filesystem.
    "d ${wolfPaths.user.modStaging} 0750 1000 1000 - -"
    "d ${wolfPaths.user.mods} 0750 1000 1000 - -"
    "d ${wolfPaths.user.backups} 0750 1000 1000 - -"
    "d ${wolfPaths.user.downloads} 0750 1000 1000 - -"
    "d ${wolfPaths.user.logs} 0750 1000 1000 - -"
    "d ${wolfPaths.user.manifests} 0750 1000 1000 - -"
    "d ${wolfPaths.user.tools} 0750 1000 1000 - -"
    "d ${wolfPaths.guest.nonsteam} 0750 1000 1000 - -"
    "d ${wolfPaths.guest.steamapps} 0750 1000 1000 - -"
    "d ${wolfPaths.guest.modStaging} 0750 1000 1000 - -"
    "d ${wolfPaths.guest.mods} 0750 1000 1000 - -"
    "d ${wolfPaths.guest.backups} 0750 1000 1000 - -"
    "d ${wolfPaths.guest.downloads} 0750 1000 1000 - -"
    "d ${wolfPaths.guest.logs} 0750 1000 1000 - -"
    "d ${wolfPaths.guest.manifests} 0750 1000 1000 - -"
    "d ${wolfPaths.guest.tools} 0750 1000 1000 - -"
    "d ${wolfState} 0750 root root - -"
    "d ${wolfHostStatePath} 0750 1000 1000 - -"
    "d /run/wolf 0755 root root - -"
  ];

  # The generated OCI service otherwise knows only about Docker/network-online.
  # Refuse to start it before encrypted state and GPU/input prerequisites exist.
  systemd.services.docker-wolf = {
    requires = [
      "srv.mount"
      "nardol-gaming-readiness.service"
    ];
    after = [
      "srv.mount"
      "nardol-gaming-readiness.service"
    ];
    serviceConfig.ExecStartPre = [ (lib.getExe wolfConfigPolicy) ];
  };

  systemd.services.nardol-vban-microphone = {
    description = "Receive the Mac VBAN microphone into Wolf PulseAudio";
    wantedBy = [ "multi-user.target" ];
    requires = [ "docker-wolf.service" ];
    after = [ "docker-wolf.service" ];
    partOf = [ "docker-wolf.service" ];
    preStart = lib.getExe vbanMicPrepare;
    script = ''
      export PULSE_SERVER=unix:${wolfPulseSocket}
      exec ${lib.getExe' vban "vban_receptor"} \
        --ipaddress=${vbanClientAddress} \
        --port=${toString vbanPort} \
        --streamname=${vbanStreamName} \
        --backend=pulseaudio \
        --device=${wolfMicSink} \
        --quality=1
    '';
    postStop = lib.getExe vbanMicCleanup;
    serviceConfig = {
      Restart = "on-failure";
      RestartSec = "2s";
      DynamicUser = true;
      UMask = "0077";
      NoNewPrivileges = true;
      PrivateDevices = true;
      PrivateTmp = true;
      ProtectHome = true;
      ProtectSystem = "strict";
      ProtectControlGroups = true;
      ProtectKernelModules = true;
      ProtectKernelTunables = true;
      RestrictAddressFamilies = [
        "AF_INET"
        "AF_UNIX"
      ];
      LockPersonality = true;
      MemoryDenyWriteExecute = true;
      CapabilityBoundingSet = "";
    };
  };

  systemd.services.nardol-gaming-readiness = {
    description = "Verify Nardol's headless NVIDIA and Wolf prerequisites";
    wantedBy = [ "multi-user.target" ];
    requires = [
      "docker.service"
      "srv.mount"
    ];
    after = [
      "docker.service"
      "nvidia-container-toolkit-cdi-generator.service"
      "nvidia-persistenced.service"
      "srv.mount"
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      test -c /dev/uinput
      test -r ${nvidiaAllocatorHostPath}
      test -c /dev/uhid
      test -c ${renderNode}
      test -r ${nvidiaEglVendorFile}
      test -r ${nvrtcLib}/lib/libnvrtc.so
      test "$(${pkgs.coreutils}/bin/cat /sys/module/nvidia_drm/parameters/modeset)" = Y
      test "$(${pkgs.coreutils}/bin/basename "$(${pkgs.coreutils}/bin/readlink -f /sys/class/drm/renderD128/device/driver)")" = nvidia
      test -s /var/run/cdi/nvidia-container-toolkit.json
      ${docker} info --format '{{json .Runtimes}}' | ${pkgs.gnugrep}/bin/grep -q '"nvidia"'
      ${nvidiaSmi} --query-gpu=name,driver_version --format=csv,noheader
    '';
  };

  # Wolf uses host networking. Expose only its documented GameStream ports;
  # there is no blanket trusted interface or Docker-published-port bypass.
  networking.firewall = {
    allowedTCPPorts = [
      47984 # HTTPS
      47989 # HTTP / pairing
      48010 # RTSP
    ];
    allowedUDPPorts = [
      47999 # control
      48100 # video
      48200 # audio
    ];
    # VBAN carries raw microphone audio and has no authentication layer. Keep
    # it on the physical LAN and accept packets only from this Mac; do not add
    # 6980 to the broad GameStream allowlist above.
    extraCommands = ''
      iptables -w -A nixos-fw \
        -i eth0 \
        -s ${vbanClientAddress}/32 \
        -p udp \
        --dport ${toString vbanPort} \
        -m comment --comment "VBAN microphone from Mac" \
        -j nixos-fw-accept
    '';
    extraStopCommands = ''
      iptables -w -D nixos-fw \
        -i eth0 \
        -s ${vbanClientAddress}/32 \
        -p udp \
        --dport ${toString vbanPort} \
        -m comment --comment "VBAN microphone from Mac" \
        -j nixos-fw-accept \
        2>/dev/null || true
    '';
  };

  assertions = [
    {
      assertion =
        wolfConfigImageLines != [ ]
        && lib.all (
          line:
          builtins.match "[[:space:]]*image[[:space:]]*=[[:space:]]*[\"'][^@\"']+@sha256:[0-9a-f]{64}[\"'][[:space:]]*" line
          != null
        ) wolfConfigImageLines;
      message = "nardol: every Wolf template child image must have a registry digest.";
    }
    {
      assertion = config.fileSystems ? "/srv";
      message = "nardol: Wolf state requires the encrypted /srv filesystem.";
    }
    {
      assertion = validateWolfConfig wolfConfigData;
      message = "nardol: every Wolf profile and whole app identity must match the reviewed two-player policy.";
    }
    {
      assertion = config.hardware.nvidia-container-toolkit.enable;
      message = "nardol: Wolf requires the NVIDIA container toolkit.";
    }
    {
      assertion = config.hardware.nvidia.modesetting.enable && config.hardware.nvidia.open;
      message = "nardol: Wolf requires NVIDIA DRM modesetting and the selected open kernel modules.";
    }
  ];
}
