{ config, lib, pkgs, sops, ... }: {
  imports =
    [ # Include the results of the hardware scan.
      ./hardware-configuration.nix
      ./disko.nix
      ./system.nix
      #./scripts.nix
      #./secrets.nix
      ../../modules/boot.nix
      ../../users/edgar/default.nix
    ];
}