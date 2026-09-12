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

    # ⛔ REQUIRED BEFORE THIS HOST MAY SUSPEND. Without it, S3 is a GPU hazard.
    #
    # This option is what installs nvidia-suspend.service, nvidia-resume.service
    # and nvidia-hibernate.service. Those units drive /proc/driver/nvidia/suspend,
    # which is how the driver saves and restores GPU state across a sleep
    # transition. Verified on the live host 2026-09-12: with this false,
    # `systemctl list-unit-files "nvidia*"` returned only persistenced and the CDI
    # generator — nothing hooked into sleep.target at all — while
    # /proc/driver/nvidia/suspend existed and went unused.
    #
    # ⚠️ nvidiaPersistenced is NOT a substitute, and it is easy to assume it is.
    # The persistence daemon keeps the device files open so the driver stays
    # loaded between clients; it does nothing to preserve or restore VRAM across
    # a suspend.
    #
    # The failure this prevents is a silent one. A resume with unsaved GPU state
    # can leave CUDA erroring, NVENC broken, or the device fallen off the bus —
    # while nardol-gaming-readiness (Type=oneshot, RemainAfterExit=true) still
    # reports active from its boot-time run and Wolf still answers on its port.
    # The host looks ready and cannot encode a frame.
    #
    # finegrained stays FALSE deliberately: that is runtime D3 power management
    # for Optimus laptops that power the GPU down between uses, which is both
    # inapplicable to a headless desktop and in conflict with nvidiaPersistenced.
    powerManagement.enable = true;

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
