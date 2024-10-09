# Do not modify this file!  It was generated by ‘nixos-generate-config’
# and may be overwritten by future invocations.  Please make changes
# to /etc/nixos/configuration.nix instead.
{ config, lib, pkgs, modulesPath, nixos-hardware, ... }:

{
  sound.enable = true;
  nixpkgs.hostPlatform.system = "aarch64-linux";
  nixpkgs.config.allowUnsupportedSystem = true;
  console.enable = false;
  boot.initrd.kernelModules = [ 
    "xhci_pci" 
    "usbhid" 
    "uas" 
    "bcm2835_rng" 
    "bcm2835_wdt"
    "usb_storage"
    "vc4"
    "pcie_brcmstb" # required for the pcie bus to work
    "reset-raspberrypi" # required for vl805 firmware to load
    ];
  boot.kernelModules = [ 
    "xhci_pci" 
    "usbhid" 
    "snd_bcm2835"
    "uas" 
    "bcm2835_rng" 
    "bcm2835_wdt"
    "usb_storage"
    "vc4"
    "pcie_brcmstb" # required for the pcie bus to work
    "reset-raspberrypi" # required for vl805 firmware to load
   # "gasket" # Kernel module for Coral USB Accelerator
   # "apex"   # Kernel module for Coral USB Accelerator
  ];
  boot.extraModulePackages = [ ];
  swapDevices = [ ];
  hardware.enableRedistributableFirmware = true;

  powerManagement.cpuFreqGovernor = lib.mkDefault "ondemand";


  # GPU
  hardware = {
    raspberry-pi = {
      "4" = {
        apply-overlays-dtmerge.enable = true;
        fkms-3d.enable = true;
      };
    };
    deviceTree = {
      enable = true;
      filter = lib.mkForce "*rpi-4-*.dtb";
    };
  };

  # eeprom
  environment.systemPackages = with pkgs; [
    libraspberrypi
    raspberrypi-eeprom
  ];

  # Audio
  hardware.pulseaudio.enable = true;

  #Bluetooth
  systemd.services.btattach = {
    before = [ "bluetooth.service" ];
    after = [ "dev-ttyAMA0.device" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      ExecStart = "${pkgs.bluez}/bin/btattach -B /dev/ttyAMA0 -P bcm -S 3000000";
    };
  };
 # Automate update PI firmware
  environment.shellAliases = {
      raspi-cpu = ''
        sudo vcgencmd get_throttled && sudo vcgencmd measure_temp
      '';
      raspi-firmware-update = ''
        sudo mkdir -p /mnt && \
        sudo mount /dev/disk/by-label/FIRMWARE /mnt && \
        BOOTFS=/mnt FIRMWARE_RELEASE_STATUS=stable sudo -E rpi-eeprom-update -d -a && \
        sudo umount /mnt
      '';
    };
  
}