{ config, lib, pkgs, sops, ... }: {
  imports =
    [ # Include the results of the hardware scan.
      ./hardware-configuration.nix
      ./disko.nix
      ./system.nix
      ./scripts.nix
      ../../users/edgar/default.nix
    ];

  # Use the GRUB 2 boot loader.
  #boot.loader.grub.enable = true;
  #boot.loader.grub.efiSupport = true;
  #boot.loader.grub.efiInstallAsRemovable = true;

  # Use the systemd-boot boot loader.
  boot.loader.systemd-boot.enable = true;
  boot.loader.systemd-boot.configurationLimit = 10;
  boot.loader.systemd-boot.editor = false;
  boot.loader.efi.canTouchEfiVariables = true;
  boot.loader.efi.efiSysMountPoint = "/boot/efi";
  #boot.loader.systemd-boot.efi.espSize = 500;
  #boot.loader.systemd-boot.devices = [ luksDevice ];
  # networking.hostName = "nixos"; # Define your hostname.
  #boot.loader.systemd-boot.enable = lib.mkDefault true;
  #boot.loader.efi.canTouchEfiVariables = lib.mkDefault true;
  #boot.loader.timeout = lib.mkForce 1;
  #boot.supportedFilesystems = ["btrfs"];
  #boot.initrd.availableKernelModules = ["uhci_hcd" "ehci_pci" "ahci" "virtio_pci" "sr_mod" "virtio_blk"];
  #boot.kernelModules = ["kvm-intel"];

  # Set the time zone.
  time.timeZone = "America/Los_Angeles";

  # Configure network proxy
  # networking.proxy.default = "http://user:password@proxy:port/";
  # networking.proxy.noProxy = "127.0.0.1,localhost,internal.domain";

  system.stateVersion = "24.05";

}
