# pelargir — Raspberry Pi 5 home-automation control plane.
{ inputs, ... }:
{
  imports = [
    inputs.nixos-hardware.nixosModules.raspberry-pi-5
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
