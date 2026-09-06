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

    # nixpkgs' production branch at this flake pin is 595.71.05. The 595.84
    # release notes fix Crimson Desert GPU hangs, and 595.99.02 is the later
    # production maintenance release carrying that fix, so pin it explicitly
    # via mkDriver instead of tracking the older nixpkgs production branch.
    # Only nardol (x86_64) consumes this role, so no aarch64 hash is needed.
    # Source: https://www.nvidia.com/en-us/drivers/details/272964/
    package = config.boot.kernelPackages.nvidiaPackages.mkDriver {
      version = "595.99.02";
      sha256_64bit = "sha256-6HR3lYv3YwcFSTJL1a1slI66btIQ5EAFs+/4SUD24ew=";
      openSha256 = "sha256-T36x/jx8yQ8l3LFp1rZIrTfcSwbGy8YSAvXOUSptpb4=";
      settingsSha256 = "sha256-GYCcnxfKPrTCrsmd25sMyzfC5cqJQJx0c31haooyTYM=";
      persistencedSha256 = "sha256-VyKtF/HdHPQrHHK6opSO69M72LmnGZtauuchj9uuje8=";
    };
  };

  # GPU passthrough into containers (Wolf, inference). NixOS generates a CDI
  # device specification; wolf.nix also retains the compatibility runtime that
  # Wolf stable currently writes into its child-container requests.
  hardware.nvidia-container-toolkit.enable = true;
}
