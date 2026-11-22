# edoras — full-rate tensor cores on the RTX 3090 (gpu-unlock).
#
# GeForce Ampere runs FP16/BF16/TF32 tensor math with FP32 accumulation at half
# the rate of the same silicon sold as RTX A-series. gpu-unlock is a kernel
# module that, before the NVIDIA driver binds the card, writes the speed
# selectors back to full rate and resets the GPU. Upstream lists Ampere GeForce
# as stable. It flashes nothing: the unlock lasts until the card loses power,
# and this module reapplies it on every boot.
#
# What it buys is prefill: prompt and image processing. Token generation is
# memory-bound and does not change. (The sheet reader's model moved to the
# A4000 on 2026-11-20; see ./inference.nix.)
#
# ⛔ ONLY THE 3090, BY PCI ADDRESS. The A4000 is a workstation card that already
# runs at full rate, so `targets=all` would only put it through an unlock and
# reset it does not need. 01:00.0 is the 3090 in PCIEX16
# (./hardware-configuration.nix); if the slot layout ever changes, update this.
#
# The recipe is the one documented in the nixpkgs manual (CUDA section,
# "Unlocking GeForce tensor core throughput"): the module must load in the
# initrd before udev or the module loader can bind NVIDIA, which listing it in
# boot.initrd.kernelModules does not guarantee, hence the explicit unit. It
# needs systemd in the initrd, which this host has.
#
# Verify after boot. A successful unit exit alone does not mean a GPU unlocked:
#   journalctl -k -b --grep=gpu_unlock      # an "unlocked" line for 0000:01:00.0
# Disable: remove the gpu_unlock.targets parameter and reboot.
{ config, ... }:
let
  gpuUnlock = config.boot.kernelPackages.callPackage ../../../pkgs/gpu-unlock { };
in
{
  boot.extraModulePackages = [ gpuUnlock ];
  hardware.firmware = [ gpuUnlock.firmware ];
  boot.initrd.availableKernelModules = [ "gpu_unlock" ];
  boot.kernelParams = [ "gpu_unlock.targets=0000:01:00.0" ];

  boot.initrd.systemd.services.gpu-unlock = {
    description = "Unlock RTX 3090 tensor core throughput";
    wantedBy = [ "sysinit.target" ];
    before = [
      "sysinit.target"
      "systemd-modules-load.service"
      "systemd-udevd.service"
      "systemd-udev-trigger.service"
    ];
    unitConfig.DefaultDependencies = false;
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "/bin/modprobe gpu_unlock";
    };
  };
}
