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

  # mkNixos { system ? "x86_64-linux"; nixpkgs ? inputs.nixpkgs; builder ? null; modules; }
  #
  # `nixpkgs` is a parameter so unported hosts can stay pinned to an older
  # release while current hosts track the latest.
  #
  # `builder` overrides the function that constructs the system. It exists for
  # hosts whose hardware framework ships its OWN `nixosSystem` wrapper that must
  # run before the module system starts. pelargir passes
  # `inputs.nixos-raspberrypi.lib.nixosSystem`, which injects the Raspberry Pi
  # overlays (vendor kernel, firmware, `raspberrypi-utils`), sets
  # `nixpkgs.hostPlatform`, trusts the framework's prebuilt-kernel cache, and
  # supplies the `nixos-raspberrypi` module argument through specialArgs.
  # Importing those board modules WITHOUT the wrapper does not evaluate: they
  # reference vendor packages that only the wrapper's overlays provide
  # (verified 2026-08-03 — `undefined variable 'raspberrypi-utils'`).
  #
  # A `builder` host also takes its package set from the FRAMEWORK's nixpkgs
  # rather than this flake's: the framework's modules are written and binary-
  # cached against its own pin, and mixing the two is the untested combination.
  # `system` is deliberately not forwarded — the wrapper sets `hostPlatform`.
  mkNixos =
    {
      system ? "x86_64-linux",
      nixpkgs ? inputs.nixpkgs,
      builder ? null,
      modules,
    }:
    if builder != null then
      builder {
        specialArgs = { inherit inputs; };
        modules = baseModules ++ modules;
      }
    else
      nixpkgs.lib.nixosSystem {
        inherit system;
        specialArgs = { inherit inputs; };
        modules = baseModules ++ modules;
      };
}
