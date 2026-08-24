{
  config,
  lib,
  pkgs,
  wolfContainerStatePath,
  wolfHostStatePath,
  wolfImage,
  wolfImagePins,
}:
let
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
in
{
  inherit
    docker
    nvidiaAllocatorHostPath
    nvidiaEglVendorFile
    nvidiaSmi
    nvrtcLib
    renderNode
    ;

  configuration = {
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
  };
}
