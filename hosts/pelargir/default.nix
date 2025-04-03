{ config, lib, disko, pkgs, sops, nixos-hardware, ... }: {
  imports =
    [ # Include the results of the hardware scan.
      ./hardware-configuration.nix
      #./disko.nix
      ./system.nix
      ./boot.nix
      #./firmware.nix
      ../../users/edgar/default.nix
      #../../modules/zfs.nix
      #./override.nix
      ./disko-config.nix
    ];
}