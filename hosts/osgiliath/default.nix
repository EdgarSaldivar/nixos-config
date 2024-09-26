{ config, lib, pkgs, sops, ... }: {
  imports =
    [ # Include the results of the hardware scan.
      ./hardware-configuration.nix
      ./disko.nix
      ./system.nix
      ./boot.nix
      ../../users/edgar/default.nix
      ../../modules/boot.nix
      #../../modules/zfs.nix
    ];
}