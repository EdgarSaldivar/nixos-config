# pelargir — Pi 5 extlinux/U-Boot boot chain.
{ pkgs, ... }:
{
  boot = {
    loader.grub.enable = false;
    loader.generic-extlinux-compatible.enable = true;

    # Owner decision: mainline provides the Pi 5 RP1 support needed here. The
    # hardware module sees pname "linux" and consequently adds rp1_pci and
    # pinctrl-rp1; it also adds nvme, pcie-brcmstb and clk-rp1 to the initrd.
    kernelPackages = pkgs.linuxPackages_latest;

    # Vendor-kernel fallback (one line): set boot.kernelPackages to the
    # raspberry-pi-5 module default by removing the assignment above.
    tmp.cleanOnBoot = true;
  };

  # Bumped nixos-hardware owns both initial firmware population and the rebuild
  # re-sync. Its default package is raspberrypifw and its available U-Boot attr
  # in this nixpkgs is ubootRaspberryPiAarch64 (there is no Pi-5-only attr).
  hardware.raspberry-pi.firmware = {
    enable = true;
    path = "/boot/firmware";
    uboot = {
      enable = true;
      package = pkgs.ubootRaspberryPiAarch64;
    };
  };

  # Upstream deviation, verified at nixos-hardware 2e790b0: its README warns
  # that this generic U-Boot cannot yet read Pi 5 PCIe/NVMe. The configuration
  # follows the module's only extlinux path, but INSTALL-RUNBOOK treats an NVMe
  # U-Boot test as a hard reboot gate rather than pretending this is proven.
}
