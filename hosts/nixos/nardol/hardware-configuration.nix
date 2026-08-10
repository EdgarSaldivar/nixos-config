# Placeholder for a Ryzen 9 5950X / X570 Taichi / NVMe box.
#
# Regenerate on the target once installed:
#   nixos-generate-config --no-filesystems --root /mnt
#
# --no-filesystems matters: disko owns fileSystems.*, and a generated
# hardware-configuration.nix would otherwise fight it.
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
    # Intel I211 NIC. Tang and the initrd SSH fallback both require this before
    # the encrypted root is available; boot.nix asserts that it stays here.
    "igb"
    "xhci_pci"
    "ahci"
    "usbhid"
    "usb_storage"
    "sd_mod"
  ];
  boot.kernelModules = [ "kvm-amd" ];

  hardware.cpu.amd.updateMicrocode = lib.mkDefault config.hardware.enableRedistributableFirmware;
  hardware.enableRedistributableFirmware = lib.mkDefault true;

  nixpkgs.hostPlatform = lib.mkDefault "x86_64-linux";
}
