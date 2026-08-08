# Headless Games on Whales / Wolf host.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  # Stable multi-architecture manifest resolved on 2026-08-07. Update this
  # deliberately after a Moonlight smoke test instead of drifting at reboot.
  wolfImage = "ghcr.io/games-on-whales/wolf@sha256:ff82c125c9b79b2e9443de2b0eaec40c904edb03291680d408cccd57c1d59c76";
  wolfState = "/srv/wolf";
  renderNode = "/dev/dri/renderD128";
  nvidiaSmi = lib.getExe' config.hardware.nvidia.package "nvidia-smi";
  nvidiaToolkit = config.hardware.nvidia-container-toolkit.package;
  nvidiaToolkitTools = lib.getOutput "tools" nvidiaToolkit;
  nvidiaRuntime = lib.getExe' nvidiaToolkitTools "nvidia-container-runtime";
  docker = lib.getExe config.virtualisation.docker.package;
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
        HOST_APPS_STATE_FOLDER = "/var/lib/wolf";
        NVIDIA_DRIVER_CAPABILITIES = "all";
        NVIDIA_VISIBLE_DEVICES = "all";
        WOLF_DEFAULT_RUN_GID = "1000";
        WOLF_DEFAULT_RUN_UID = "1000";
        WOLF_RENDER_NODE = renderNode;
        WOLF_STOP_CONTAINER_ON_EXIT = "TRUE";
        XDG_RUNTIME_DIR = "/run/wolf";
      };
      volumes = [
        "${wolfState}/config:/etc/wolf:rw"
        "${wolfState}/data:/var/lib/wolf:rw"
        "/run/wolf:/run/wolf:rw"
        "/var/run/docker.sock:/var/run/docker.sock:rw"
        "/dev:/dev:rw"
        "/run/udev:/run/udev:rw"
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

  # Keep generated configuration/pairing material separate from large game and
  # profile state, even though both currently live on the same encrypted SSD.
  systemd.tmpfiles.rules = [
    "d /srv/docker 0710 root docker - -"
    "d ${wolfState} 0750 root root - -"
    "d ${wolfState}/config 0750 root root - -"
    "d ${wolfState}/data 0750 1000 1000 - -"
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
      test -c /dev/uhid
      test -c ${renderNode}
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
      assertion = config.fileSystems ? "/srv";
      message = "nardol: Wolf state requires the encrypted /srv filesystem.";
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
