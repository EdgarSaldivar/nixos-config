{ config, lib, pkgs, sops, ... }: {
  imports =
    [ 
      ./hardware-configuration.nix
      ./disko.nix
      ./system.nix
      #./scripts.nix
      ./boot.nix
      ../../users/edgar/default.nix
    ];

}