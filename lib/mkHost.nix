# Host construction helpers.
#
# Keeps flake.nix free of the repeated `modules = [ disko home-manager sops ... ]`
# list that every host used to spell out by hand.
{ inputs }:
rec {
  # Modules every NixOS host receives.
  baseModules = [
    ../modules/nixos/profiles/base.nix
    inputs.disko.nixosModules.disko
    inputs.home-manager.nixosModules.home-manager
    inputs.sops.nixosModules.sops
    # Keep this beside host construction: inputs.self is already in scope here,
    # and every deployment then identifies its exact clean or dirty flake rev.
    ({ ... }: {
      system.configurationRevision = inputs.self.rev or inputs.self.dirtyRev or null;
    })
  ];

  # mkNixos { system ? null; nixpkgs ? inputs.nixpkgs; builder ? null; modules; }
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
  # `system` is forbidden for builder hosts because the wrapper sets
  # `hostPlatform`; accepting and ignoring it would make the call site lie.
  # Conversely, ordinary nixpkgs hosts must pass `system` explicitly so a new
  # architecture can never silently become x86_64-linux.
  mkNixos =
    {
      system ? null,
      nixpkgs ? inputs.nixpkgs,
      builder ? null,
      modules,
    }:
    if builder != null then
      if system != null then
        throw "mkNixos: builder hosts set hostPlatform themselves; omit system"
      else
        builder {
          specialArgs = { inherit inputs; };
          modules = baseModules ++ modules;
        }
    else if system == null then
      throw "mkNixos: non-builder hosts must pass system explicitly"
    else
      nixpkgs.lib.nixosSystem {
        inherit system;
        specialArgs = { inherit inputs; };
        modules = baseModules ++ modules;
      };
}
