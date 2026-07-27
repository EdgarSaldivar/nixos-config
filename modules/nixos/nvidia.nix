# NVIDIA proprietary driver + container GPU access.
{ config, ... }:
{
  hardware.graphics = {
    enable = true;
    enable32Bit = true; # required by Steam/Proton
  };

  services.xserver.videoDrivers = [ "nvidia" ];

  hardware.nvidia = {
    modesetting.enable = true;
    nvidiaSettings = true;

    # Stay on the proprietary module: the open kernel module still has rough
    # edges around CUDA and container workloads.
    open = false;

    package = config.boot.kernelPackages.nvidiaPackages.production;
  };

  # GPU passthrough into containers (Wolf, inference).
  #
  # `--gpus all` was broken on 25.05/25.11; fixed by nixpkgs#485584 (merged to
  # master 2026-02-02), which 26.05 inherits. Plain `--gpus all` works again —
  # the `--device nvidia.com/gpu=all` CDI workaround is no longer needed.
  hardware.nvidia-container-toolkit.enable = true;
}
