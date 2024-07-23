{ config, lib, pkgs, sops, ... }: {
  imports =
    [ # Include the results of the hardware scan.
      ./hardware-configuration.nix
      ./disko.nix
      ./system.nix
      ./scripts.nix
      ../../users/edgar/default.nix
    ];

  # Use the systemd-boot boot loader.
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;
  # networking.hostName = "nixos"; # Define your hostname.

  # Set the time zone.
  time.timeZone = "America/Los_Angeles";

  # Configure network proxy
  # networking.proxy.default = "http://user:password@proxy:port/";
  # networking.proxy.noProxy = "127.0.0.1,localhost,internal.domain";

  system.stateVersion = "24.05";

}
