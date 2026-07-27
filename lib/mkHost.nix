# Host construction helpers.
#
# Keeps flake.nix free of the repeated `modules = [ disko home-manager sops ... ]`
# list that every host used to spell out by hand.
{ inputs }:
rec {
  # Modules every NixOS host receives.
  baseModules = [
    inputs.disko.nixosModules.disko
    inputs.home-manager.nixosModules.home-manager
    inputs.sops.nixosModules.sops
  ];

  # mkNixos { system ? "x86_64-linux"; nixpkgs ? inputs.nixpkgs; modules; }
  #
  # `nixpkgs` is a parameter so unported hosts can stay pinned to an older
  # release while current hosts track the latest.
  mkNixos =
    {
      system ? "x86_64-linux",
      nixpkgs ? inputs.nixpkgs,
      modules,
    }:
    nixpkgs.lib.nixosSystem {
      inherit system;
      specialArgs = { inherit inputs; };
      modules = baseModules ++ modules;
    };
}
