# Validated only on nardol: this single-consumer role bundle is named for its
# headless NVIDIA container-host role, not a general NVIDIA capability.
# Graphics, the NVIDIA selector, modesetting, persistence, production driver,
# and container toolkit are reusable on a modern headless NVIDIA container
# host. Before a second host imports it, parameterise gaming-specific
# enable32Bit and `open = true`, which assumes nardol's RTX 4090 generation.

# NVIDIA userspace driver, open kernel modules, and container GPU access.
{ config, ... }:
{
  hardware.graphics = {
    enable = true;
    enable32Bit = true; # required by Steam/Proton
  };

  services.xserver.videoDrivers = [ "nvidia" ];

  hardware.nvidia = {
    modesetting.enable = true;
    nvidiaSettings = false; # no GUI utility on this headless host
    nvidiaPersistenced = true;

    # RTX 4090 is Ada (well past Turing). NVIDIA recommends the open kernel
    # modules for Turing and newer, and they are the default flavor upstream.
    # Userspace remains NVIDIA's full gaming/CUDA/NVENC driver either way.
    open = true;

    # Track nixpkgs' production branch instead of beta/vulkan-beta. At this
    # flake pin production, stable, and latest all resolve to 595.71.05.
    package = config.boot.kernelPackages.nvidiaPackages.production;
  };

  # GPU passthrough into containers (Wolf, inference). NixOS generates a CDI
  # device specification; wolf.nix also retains the compatibility runtime that
  # Wolf stable currently writes into its child-container requests.
  hardware.nvidia-container-toolkit.enable = true;
}
