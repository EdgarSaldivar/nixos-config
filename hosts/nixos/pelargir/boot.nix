# pelargir — Pi 5 GPU-firmware direct boot from NVMe.
{ ... }:
{
  # Verified in raspberry-pi-nix 3e8100d: bcm2712 selects the Pi 5 vendor
  # linux-rpi package, direct boot is the default, and the module installs
  # kernel.img, initrd, cmdline.txt, generated config.txt, DTBs, and firmware.
  # It also enables NixOS's init-script loader: the current generation is
  # /sbin/init and older generation launchers are recorded under /boot.
  raspberry-pi-nix = {
    board = "bcm2712";
    firmware-partition-label = "FIRMWARE";
    uboot.enable = false;
  };

  # linuxPackages_latest/mainline remains possible by disabling the module's
  # pinned inputs and overriding boot.kernelPackages, but that gives up the
  # vendor defconfig and nix-community cache identity and may require restoring
  # Pi 5/RP1/initrd support manually. Owner decision: use the cached vendor
  # kernel convention provided by raspberry-pi-nix.
  boot = {
    # Verified at raspberry-pi-nix 3e8100d: its common stage-1 list includes
    # pcie_brcmstb but not nvme or clk-rp1. Keep the complete Pi 5 NVMe path
    # explicit so initrd can discover the root filesystem before switch-root.
    # Hyphen/underscore module spellings are equivalent to modprobe.
    initrd.availableKernelModules = [
      "nvme"
      "pcie-brcmstb"
      "clk-rp1"
    ];

    tmp.cleanOnBoot = true;
  };

  # No config.txt PCIe override is intentional. Raspberry Pi OS on this exact
  # Pi 5 + Argon V5 dual-M.2 board auto-detected NVMe without pciex1 dtparam
  # (verified live 2026-08-03). The ASM1182e is a Gen2 switch, so forcing Gen3
  # would add risk without increasing link speed.
}
