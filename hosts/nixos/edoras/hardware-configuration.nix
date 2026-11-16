# edoras — hardware, PROVISIONAL until first boot.
#
# The system is installed from nardol with the NVMe in a USB enclosure, before
# it ever sees its own motherboard, so nothing here could be probed. It is the
# generic set any x86_64 desktop boots with: NVMe/SATA/USB storage in the
# initrd, KVM for either CPU vendor, and microcode for both (the loader applies
# only the matching one).
#
# After first boot, replace this file with the probed inventory:
#   sudo nixos-generate-config --no-filesystems --show-hardware-config
# and record CPU, board, RAM, NIC and both GPUs in this header, as osgiliath does.
#
# GPU  : RTX 3090 (24 GB, Ampere) + RTX A4000 (16 GB, Ampere)
# Root : Intel 660p 1 TB NVMe, SSDPEKNW010T8, serial BTNH10350EER1P0B (the only
#        disko-managed device; see ./disko.nix). QLC and DRAM-less: fine for
#        read-mostly model weights, wrong for write churn such as build caches.
#        SMART 2026-11-15: 0% used, 3.29 TB written of 200 TBW, no media errors.
{
  config,
  lib,
  modulesPath,
  ...
}:
{
  imports = [ (modulesPath + "/installer/scan/not-detected.nix") ];

  boot.initrd.availableKernelModules = [
    "nvme"
    "xhci_pci"
    "ahci"
    "usbhid"
    "usb_storage"
    "uas"
    "sd_mod"
  ];
  boot.initrd.kernelModules = [ ];
  boot.kernelModules = [
    "kvm-amd"
    "kvm-intel"
  ];
  boot.extraModulePackages = [ ];

  hardware.enableRedistributableFirmware = true;
  hardware.cpu.amd.updateMicrocode = lib.mkDefault config.hardware.enableRedistributableFirmware;
  hardware.cpu.intel.updateMicrocode = lib.mkDefault config.hardware.enableRedistributableFirmware;

  swapDevices = [ ];
  nixpkgs.hostPlatform = lib.mkDefault "x86_64-linux";
}
