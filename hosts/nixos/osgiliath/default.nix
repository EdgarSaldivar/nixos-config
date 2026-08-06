# osgiliath — GMKtec NucBox G10 Frigate host foundation.
{ ... }:
{
  imports = [
    ./hardware-configuration.nix
    ./disko.nix
    ./boot.nix
    ./system.nix
    ./hardware-services.nix
    ./secrets.nix
    ./wifi.nix
    ./k3s.nix
    ./workload-storage.nix
    ../../../users/edgar/default.nix
  ];
}
