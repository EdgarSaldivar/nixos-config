{
  description = "Edgar's NixOS and nix-darwin configurations";

  inputs = {
    # Current release. New and ported hosts track this.
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

    # Darwin needs the nixpkgs-*-darwin branch, NOT nixos-*. Mixing the two
    # is what previously broke the Linux hosts: every host was pointed at
    # nixpkgs-24.11-darwin to make dol-amroth work.
    #
    # Kept ALIGNED with the Linux release (26.05). Darwin sat on 24.11 while
    # Linux moved to 26.05, which meant the two halves of this flake were 18
    # months apart and dol-amroth was quietly running an unmaintained release.
    nixpkgs-darwin.url = "github:NixOS/nixpkgs/nixpkgs-26.05-darwin";

    nix-darwin = {
      url = "github:nix-darwin/nix-darwin/nix-darwin-26.05";
      inputs.nixpkgs.follows = "nixpkgs-darwin";
    };

    disko = {
      url = "github:nix-community/disko";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    home-manager = {
      url = "github:nix-community/home-manager/release-26.05";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    sops = {
      url = "github:Mic92/sops-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    nixos-hardware.url = "github:NixOS/nixos-hardware";
  };

  outputs =
    inputs@{ nixpkgs, nix-darwin, ... }:
    let
      inherit (import ./lib/mkHost.nix { inherit inputs; }) mkNixos;
      lib = nixpkgs.lib;
      # Everything here must be EVALUATED on a Mac, so checks are eval-only and
      # never realize a Linux derivation.
      devSystem = "aarch64-darwin";
      devPkgs = nixpkgs.legacyPackages.${devSystem};
    in
    rec {
      nixosConfigurations = {
        nardol = mkNixos {
          modules = [ ./hosts/nixos/nardol ];
        };

        # Ported to 26.05 on 2026-07-30 as part of the raz-server -> NixOS
        # rebuild. Real hardware (ASRock Rack X570D4U / 5950X), not the old VM
        # skeleton. See hosts/nixos/minas-tirith/disko.nix before any disk work:
        # nine of its ten drives are live ZFS pool members.
        minas-tirith = mkNixos {
          modules = [ ./hosts/nixos/minas-tirith ];
        };

        # Remaining unported hosts (builder-vm, minas-tirith-vm, osgiliath[-vm],
        # pelargir[-vm]) are deliberately not wired up. Their sources are
        # still in hosts/nixos/, and the last state where they were declared
        # is on the `legacy/24.11` branch. Revive them one at a time by
        # porting to 26.05, rather than dragging eight broken hosts forward.
      };

      darwinConfigurations = {
        dol-amroth = nix-darwin.lib.darwinSystem {
          system = "aarch64-darwin";
          modules = [ ./hosts/darwin/dol-amroth ];
        };
      };

      formatter.${devSystem} = devPkgs.nixfmt-rfc-style;

      # -----------------------------------------------------------------------
      # checks — invariants that must hold on EVERY change. `nix flake check`.
      # -----------------------------------------------------------------------
      # These exist because the most dangerous things in this repo were verified
      # by hand, repeatedly, and a hand check is only as good as the last person
      # who remembered to run it. Both checks below encode a mistake that was
      # actually made here.
      checks.${devSystem} = {
        # A flake output named `foo` whose networking.hostName is `bar` produces
        # a machine you deploy by one name and that calls itself another. Caught
        # exactly this: dol-amroth was configured as "dol-amorth" for months.
        hostnames =
          let
            mismatched = lib.filterAttrs (n: c: c.config.networking.hostName != n) (
              nixosConfigurations // darwinConfigurations
            );
            names = lib.mapAttrsToList (n: c: "${n} -> ${c.config.networking.hostName}") mismatched;
          in
          if mismatched == { } then
            devPkgs.runCommand "hostnames-ok" { } "touch $out"
          else
            throw "flake output name != networking.hostName for: ${lib.concatStringsSep ", " names}";

        # THE important one. disko destroys exactly the disks it is handed, so
        # the only thing standing between a config edit and nine live ZFS pool
        # members (~98 TB, no backup) is that this list stays correct. Assert it
        # mechanically instead of re-reading it by eye before each install.
        minas-tirith-disko-targets =
          let
            disks = nixosConfigurations.minas-tirith.config.disko.devices.disk;
            devices = lib.mapAttrsToList (_: d: d.device) disks;
            expected = [ "/dev/disk/by-id/nvme-Samsung_SSD_980_1TB_S64ANS0RA05335R" ];
            zpools = nixosConfigurations.minas-tirith.config.disko.devices.zpool or { };
          in
          if devices != expected then
            throw "minas-tirith disko would touch ${toString devices} — expected exactly ${toString expected}"
          else if zpools != { } then
            throw "minas-tirith declares disko.devices.zpool (${toString (builtins.attrNames zpools)}) — disko must never manage the existing pools"
          else
            devPkgs.runCommand "minas-tirith-disko-targets-ok" { } "touch $out";
      };
    };
}
