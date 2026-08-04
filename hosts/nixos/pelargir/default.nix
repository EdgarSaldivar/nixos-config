# pelargir — Raspberry Pi 5 home-automation control plane.
{ inputs, ... }:
{
  imports = [
    inputs.raspberry-pi-nix.nixosModules.raspberry-pi
    ./disko.nix
    ./boot.nix
    ./system.nix
    ./secrets.nix
    ./wireguard.nix
    ./k3s.nix
    ./manifests.nix
    ./backup.nix
    ../../../users/edgar/default.nix
  ];
}
