# osgiliath — GMKtec NucBox G10 hardware inventory.
#
# CPU/GPU : AMD Ryzen 5 3500U (Picasso) with Vega integrated graphics
# RAM     : 12 GiB
# Root    : AirDisk 1 TB NVMe (the only disko-managed device)
# LAN     : RTL8125, r8169, 84:47:09:6a:b2:5d
# Wi-Fi   : RTL8822CE, rtw88_8822ce, 9c:12:21:ac:79:82
# USB     : Coral Accelerator 18d1:9302 and preserved Sabrent Frigate SSD
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
    "amdgpu"
    "r8169"
    "rtw88_8822ce"
    "btusb"
  ];
  boot.extraModulePackages = [ ];

  hardware.enableRedistributableFirmware = true;
  hardware.cpu.amd.updateMicrocode = lib.mkDefault config.hardware.enableRedistributableFirmware;
  hardware.graphics.enable = true;

  swapDevices = [ ];
  nixpkgs.hostPlatform = lib.mkDefault "x86_64-linux";
}
