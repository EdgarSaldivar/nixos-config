# fleet.nvidiaHeadless — NVIDIA driver, open kernel modules and container GPU
# access for a headless x86_64 host.
#
# Graduated from modules/nixos/roles/ when edoras became its second consumer
# (modules/README.md). Consumers: nardol (RTX 4090, Ada) and edoras (RTX 3090 +
# RTX A4000, both Ampere).
#
# The one gaming-specific piece is an option rather than a default:
# enable32Bit exists for Steam/Proton, so only a host that runs games sets it.
{ config, lib, ... }:
let
  cfg = config.fleet.nvidiaHeadless;
in
{
  options.fleet.nvidiaHeadless = {
    enable = lib.mkEnableOption "the headless NVIDIA driver, open kernel modules and container GPU access";

    enable32Bit = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Install the 32-bit graphics userspace. Steam and Proton require it, so
        a game-streaming host sets this; an inference-only host leaves it off.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    hardware.graphics = {
      enable = true;
      inherit (cfg) enable32Bit;
    };

    services.xserver.videoDrivers = [ "nvidia" ];

    hardware.nvidia = {
      modesetting.enable = true;
      nvidiaSettings = false; # no GUI utility on a headless host
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

      # Every consumer is Turing or newer: nardol's RTX 4090 is Ada, edoras's
      # RTX 3090 and RTX A4000 are Ampere. NVIDIA recommends the open kernel
      # modules from Turing on, and they are the default flavor upstream.
      # Userspace remains NVIDIA's full CUDA/NVENC driver either way. A host
      # with a pre-Turing card cannot use this module as written.
      open = true;

      # nixpkgs' production branch at this flake pin is 595.71.05. The 595.84
      # release notes fix Crimson Desert GPU hangs, and 595.99.02 is the later
      # production maintenance release carrying that fix, so pin it explicitly
      # via mkDriver instead of tracking the older nixpkgs production branch.
      # Every consumer is x86_64, so no aarch64 hash is needed.
      # Source: https://www.nvidia.com/en-us/drivers/details/272964/
      package = config.boot.kernelPackages.nvidiaPackages.mkDriver {
        version = "595.99.02";
        sha256_64bit = "sha256-6HR3lYv3YwcFSTJL1a1slI66btIQ5EAFs+/4SUD24ew=";
        openSha256 = "sha256-T36x/jx8yQ8l3LFp1rZIrTfcSwbGy8YSAvXOUSptpb4=";
        settingsSha256 = "sha256-GYCcnxfKPrTCrsmd25sMyzfC5cqJQJx0c31haooyTYM=";
        persistencedSha256 = "sha256-VyKtF/HdHPQrHHK6opSO69M72LmnGZtauuchj9uuje8=";
      };
    };

    # GPU passthrough into containers (Wolf and inference on nardol). NixOS
    # generates a CDI device specification; nardol's wolf.nix also retains the
    # compatibility runtime that Wolf stable writes into its child-container
    # requests.
    hardware.nvidia-container-toolkit.enable = true;
  };
}
