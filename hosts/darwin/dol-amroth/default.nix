{ config, lib, pkgs, sops, nixpkgs, ... }: {
  imports =
    [ # Include the results of the hardware scan.
      ./system.nix
      #../../../users/edgar/default.nix
    ];
}