{ config, lib, pkgs, sops, nixos-hardware, ... }: {
  imports =
    [ # Include the results of the hardware scan.
      ./hardware-configuration.nix
      #./disko.nix
      ./system.nix
      #./boot.nix
      #../../modules/boot.nix
      #./firmware.nix
      ../../users/edgar/default.nix
      #../../modules/zfs.nix
    ];
}