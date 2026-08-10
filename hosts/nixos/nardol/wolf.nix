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
  wolfImageTags = builtins.attrNames wolfImagePins;
  wolfConfigText = builtins.replaceStrings wolfImageTags (map (
    tag: wolfImagePins.${tag}
  ) wolfImageTags) (builtins.readFile ./wolf-config.template.toml);
  wolfConfigImageLines = builtins.filter (
    line: builtins.match "[[:space:]]*image[[:space:]]*=.*" line != null
  ) (lib.splitString "\n" wolfConfigText);
  wolfConfigTemplate = pkgs.writeText "nardol-wolf-config.toml" wolfConfigText;
  wolfImageRewrites = wolfImagePins // wolfImageMigrations;
  wolfImageRewriteSources = builtins.attrNames wolfImageRewrites;
  wolfPinSedArgs = lib.concatMapStringsSep " " (
    source:
    lib.concatStringsSep " " [
      "-e ${lib.escapeShellArg "s|\"${source}\"|\"${wolfImageRewrites.${source}}\"|g"}"
      "-e ${lib.escapeShellArg "s|'${source}'|'${wolfImageRewrites.${source}}'|g"}"
    ]
  ) wolfImageRewriteSources;
  wolfState = "/srv/wolf";
  wolfHostStatePath = "${wolfState}/data";
  wolfContainerStatePath = "/var/lib/wolf";
  renderNode = "/dev/dri/renderD128";
  nvidiaEglVendorFile = "/run/opengl-driver/share/glvnd/egl_vendor.d/10_nvidia.json";
  wolfEglVendorFiles = "${nvidiaEglVendorFile}:/usr/share/glvnd/egl_vendor.d/50_mesa.json";
  steamEglVendorFiles = "/usr/share/glvnd/egl_vendor.d/10_nvidia.json:/usr/share/glvnd/egl_vendor.d/50_mesa.json";
  nvidiaAllocatorHostPath = "/run/opengl-driver/lib/libnvidia-allocator.so.1";
  nvidiaAllocatorContainerPath = "/usr/lib/x86_64-linux-gnu/libnvidia-allocator.so.1";
  nvidiaAllocatorMount = "${nvidiaAllocatorHostPath}:${nvidiaAllocatorContainerPath}:ro";
  legacySteamEnv = "        env = [ 'PROTON_LOG=1', 'RUN_SWAY=true', 'GOW_REQUIRED_DEVICES=/dev/input/* /dev/dri/* /dev/nvidia*' ]";
  obsoleteManagedSteamEnv = "        env = [ 'PROTON_LOG=1', 'RUN_SWAY=true', 'GOW_REQUIRED_DEVICES=/dev/input/* /dev/dri/* /dev/nvidia*', '__EGL_VENDOR_LIBRARY_FILENAMES=${steamEglVendorFiles}' ]";
  managedSteamEnv = "        env = [ 'PROTON_LOG=1', 'RUN_SWAY=true', 'GOW_REQUIRED_DEVICES=/dev/input/* /dev/dri/* /dev/nvidia*', '__EGL_VENDOR_LIBRARY_FILENAMES=${steamEglVendorFiles}', 'STEAM_DIR=/home/retro/.steam/steam' ]";
  legacySteamMounts = "        mounts = []";
  obsoleteManagedSteamMounts = "        mounts = [ '/srv/games/steamapps:/home/retro/.steam/debian-installation/steamapps:rw', '/srv/mods:/home/retro/Mods:rw', '${nvidiaAllocatorMount}' ]";
  obsoleteManagedSteamMountsWithActiveRoot = "        mounts = [ '/srv/games/steamapps:/home/retro/.steam/steam/steamapps:rw', '/srv/mods:/home/retro/Mods:rw', '${nvidiaAllocatorMount}' ]";
  managedSteamMounts = "        mounts = [ '/srv/games/steamapps:/home/retro/.steam/steam/steamapps:rw', '/srv/games/steamapps:/home/retro/SteamLibrary:rw', '/srv/mods:/home/retro/Mods:rw', '${nvidiaAllocatorMount}' ]";
  legacyXfceMounts = "        mounts = []";
  managedXfceMounts = "        mounts = [ '/srv/games/steamapps:/home/retro/SteamLibrary:rw', '/srv/mods:/home/retro/Mods:rw', '/srv/mods/downloads:/home/retro/Downloads:rw' ]";
  nvrtcLib = pkgs.cudaPackages.cuda_nvrtc.lib;
  nvrtcContainerPath = "/opt/nardol-nvrtc";
  nvidiaSmi = lib.getExe' config.hardware.nvidia.package "nvidia-smi";
  nvidiaToolkit = config.hardware.nvidia-container-toolkit.package;
  nvidiaToolkitTools = lib.getOutput "tools" nvidiaToolkit;
  nvidiaRuntime = lib.getExe' nvidiaToolkitTools "nvidia-container-runtime";
  docker = lib.getExe config.virtualisation.docker.package;
  wolfConfigPolicy = pkgs.writeShellApplication {
    name = "nardol-wolf-config-policy";
    text = ''
      # Wolf derives WOLF_CFG_FOLDER from HOST_APPS_STATE_FOLDER, so the
      # controller configuration and pairing material live beside app state.
      config_dir=${wolfHostStatePath}/cfg
      config_file="$config_dir/config.toml"

      ${pkgs.coreutils}/bin/install -d -m 0750 "$config_dir"
      if ! test -e "$config_file"; then
        config_tmp="$(${pkgs.coreutils}/bin/mktemp "$config_dir/.config.toml.XXXXXX")"
        trap '${pkgs.coreutils}/bin/rm -f "$config_tmp"' EXIT
        {
          printf '# A unique identifier for this host\nuuid = "%s"\n' \
            "$(${lib.getExe' pkgs.util-linux "uuidgen"})"
          ${pkgs.coreutils}/bin/cat ${wolfConfigTemplate}
        } >"$config_tmp"
        ${pkgs.coreutils}/bin/chmod 0600 "$config_tmp"
        ${pkgs.coreutils}/bin/mv "$config_tmp" "$config_file"
        trap - EXIT
      fi

      # Upgrade only reviewed template tags and explicit one-way migrations.
      # This safely normalises a restored Triforce or pre-toolbox config without
      # touching pairing keys, clients, application state, or unknown images.
      config_tmp="$(${pkgs.coreutils}/bin/mktemp "$config_dir/.config.toml.XXXXXX")"
      trap '${pkgs.coreutils}/bin/rm -f "$config_tmp"' EXIT
      ${pkgs.gnused}/bin/sed \
        ${wolfPinSedArgs} \
        "$config_file" \
        | ${pkgs.gawk}/bin/awk \
          -v legacy_env=${lib.escapeShellArg legacySteamEnv} \
          -v obsolete_managed_env=${lib.escapeShellArg obsoleteManagedSteamEnv} \
          -v managed_env=${lib.escapeShellArg managedSteamEnv} \
          -v legacy_mounts=${lib.escapeShellArg legacySteamMounts} \
          -v obsolete_managed_mounts=${lib.escapeShellArg obsoleteManagedSteamMounts} \
          -v obsolete_managed_mounts_active=${lib.escapeShellArg obsoleteManagedSteamMountsWithActiveRoot} \
          -v managed_mounts=${lib.escapeShellArg managedSteamMounts} \
          -v legacy_xfce_mounts=${lib.escapeShellArg legacyXfceMounts} \
          -v managed_xfce_mounts=${lib.escapeShellArg managedXfceMounts} \
          '
            /^[[:space:]]*\[\[profiles\.apps\]\][[:space:]]*$/ { in_steam = 0; in_xfce = 0 }
            /^[[:space:]]*title[[:space:]]*=[[:space:]]*.*Steam.*[[:space:]]*$/ { in_steam = 1 }
            /^[[:space:]]*title[[:space:]]*=[[:space:]]*.*Desktop \(xfce\).*[[:space:]]*$/ { in_xfce = 1 }
            in_steam && ($0 == legacy_env || $0 == obsolete_managed_env) { print managed_env; next }
            in_steam && ($0 == legacy_mounts || $0 == obsolete_managed_mounts || $0 == obsolete_managed_mounts_active) { print managed_mounts; next }
            in_xfce && $0 == legacy_xfce_mounts { print managed_xfce_mounts; next }
            { print }
          ' >"$config_tmp"
      ${pkgs.coreutils}/bin/chown --reference="$config_file" "$config_tmp"
      ${pkgs.coreutils}/bin/chmod --reference="$config_file" "$config_tmp"
      if ${pkgs.diffutils}/bin/cmp --silent "$config_file" "$config_tmp"; then
        ${pkgs.coreutils}/bin/rm "$config_tmp"
      else
        ${pkgs.coreutils}/bin/mv "$config_tmp" "$config_file"
      fi
      trap - EXIT

      # Refuse to start if the single Steam app is missing any part of the
      # reviewed image, persistent-state, or NixOS NVIDIA compatibility fix.
      if ! ${pkgs.gawk}/bin/awk \
        -v expected_image=${lib.escapeShellArg steamToolsImage} \
        -v expected_egl=${lib.escapeShellArg "__EGL_VENDOR_LIBRARY_FILENAMES=${steamEglVendorFiles}"} \
        -v expected_steam_dir='STEAM_DIR=/home/retro/.steam/steam' \
        -v expected_allocator=${lib.escapeShellArg nvidiaAllocatorMount} \
        -v expected_steamapps='/srv/games/steamapps:/home/retro/.steam/steam/steamapps:rw' \
        -v expected_steamapps_alias='/srv/games/steamapps:/home/retro/SteamLibrary:rw' \
        -v expected_mods='/srv/mods:/home/retro/Mods:rw' \
        -v expected_xfce_steamapps='/srv/games/steamapps:/home/retro/SteamLibrary:rw' \
        -v expected_xfce_mods='/srv/mods:/home/retro/Mods:rw' \
        -v expected_xfce_downloads='/srv/mods/downloads:/home/retro/Downloads:rw' \
        '
          /^[[:space:]]*\[\[profiles\.apps\]\][[:space:]]*$/ { in_steam = 0; in_xfce = 0 }
          /^[[:space:]]*title[[:space:]]*=[[:space:]]*.*Steam.*[[:space:]]*$/ {
            in_steam = 1
            steam_apps++
          }
          /^[[:space:]]*title[[:space:]]*=[[:space:]]*.*Desktop \(xfce\).*[[:space:]]*$/ {
            in_xfce = 1
            xfce_apps++
          }
          in_steam {
            if (index($0, expected_image)) image_ok = 1
            if (index($0, expected_egl)) egl_ok = 1
            if (index($0, expected_steam_dir)) steam_dir_ok = 1
            if (index($0, expected_allocator)) allocator_ok = 1
            if (index($0, expected_steamapps)) steamapps_ok = 1
            if (index($0, expected_steamapps_alias)) steamapps_alias_ok = 1
            if (index($0, expected_mods)) mods_ok = 1
          }
          in_xfce {
            if (index($0, expected_xfce_steamapps)) xfce_steamapps_ok = 1
            if (index($0, expected_xfce_mods)) xfce_mods_ok = 1
            if (index($0, expected_xfce_downloads)) xfce_downloads_ok = 1
          }
          END {
            exit !(
              steam_apps == 1 && image_ok && egl_ok && steam_dir_ok && allocator_ok && steamapps_ok && steamapps_alias_ok && mods_ok
              && xfce_apps == 1 && xfce_steamapps_ok && xfce_mods_ok && xfce_downloads_ok
            )
          }
        ' "$config_file"
      then
        echo "Wolf Steam or XFCE is missing its reviewed image, persistent mounts, or NVIDIA EGL compatibility settings." >&2
        exit 1
      fi

      # Wolf and Wolf UI both control Docker. Refuse to start if a restored or
      # newly edited app would silently pull a mutable child image.
      if ${pkgs.gnugrep}/bin/grep -E '^[[:space:]]*image[[:space:]]*=' "$config_file" \
        | ${pkgs.gnugrep}/bin/grep -Ev "^[[:space:]]*image[[:space:]]*=[[:space:]]*[\"'][^@\"']+@sha256:[0-9a-f]{64}[\"'][[:space:]]*(#.*)?$"; then
        echo "Wolf config contains an unpinned child image; add its digest to wolfImagePins." >&2
        exit 1
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
    "d /srv/games/steamapps 0750 1000 1000 - -"
    # Hardlink/atomic-rename deployers need staging inside the same container
    # mount as their game targets, not merely on the same host filesystem.
    "d /srv/games/steamapps/.nardol-mod-staging 0750 1000 1000 - -"
    "d /srv/mods 0750 1000 1000 - -"
    "d /srv/mods/backups 0750 1000 1000 - -"
    "d /srv/mods/downloads 0750 1000 1000 - -"
    "d /srv/mods/logs 0750 1000 1000 - -"
    "d /srv/mods/manifests 0750 1000 1000 - -"
    "d /srv/mods/tools 0750 1000 1000 - -"
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
      assertion =
        lib.hasInfix "/srv/games/steamapps:/home/retro/.steam/steam/steamapps:rw" wolfConfigText
        && lib.hasInfix "/srv/mods:/home/retro/Mods:rw" wolfConfigText
        && lib.hasInfix nvidiaAllocatorMount wolfConfigText
        && lib.hasInfix "__EGL_VENDOR_LIBRARY_FILENAMES=${steamEglVendorFiles}" wolfConfigText
        && lib.hasInfix "STEAM_DIR=/home/retro/.steam/steam" wolfConfigText
        && lib.hasInfix "/srv/games/steamapps:/home/retro/SteamLibrary:rw" wolfConfigText
        && lib.hasInfix "/srv/mods/downloads:/home/retro/Downloads:rw" wolfConfigText
        && lib.hasInfix "image = \"${steamToolsImage}\"" wolfConfigText;
      message = "nardol: the Wolf Steam and XFCE templates must retain their reviewed persistent games, mods, downloads, and NVIDIA compatibility settings.";
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
