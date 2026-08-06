{
  description = "Edgar's NixOS and nix-darwin configurations";

  # nixos-raspberrypi 67616c2 publishes its matched vendor kernels in its own
  # Cachix. Retain nix-community for the rest of this flake; INSTALL-RUNBOOK
  # must mirror both trusts before a fresh installer evaluates the flake.
  nixConfig = {
    extra-substituters = [
      "https://nix-community.cachix.org"
      "https://nixos-raspberrypi.cachix.org"
    ];
    extra-trusted-public-keys = [
      "nix-community.cachix.org-1:mB9FSh9qf2dCimDSUo8Zy7bkq5CX+/rkCWyvRCYg3Fs="
      "nixos-raspberrypi.cachix.org-1:4iMO9LXa8BqhU+Rpg6LQKiGa2lsNh/j2oiYLNOQ5sPI="
    ];
  };

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

    nixos-raspberrypi = {
      # Pin the 2026-08-01 default-branch tip: 67616c2 makes the matched Pi
      # vendor kernel/firmware bundle default to 6.18.39. A full rev keeps the
      # direct-NVMe boot contract reviewable instead of drifting with upstream.
      url = "github:nvmd/nixos-raspberrypi/67616c24ed74573750f4864abfc358296a077466";
    };
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
          system = "x86_64-linux";
          modules = [ ./hosts/nixos/nardol ];
        };

        # Ported to 26.05 on 2026-07-30 as part of the raz-server -> NixOS
        # rebuild. Real hardware (ASRock Rack X570D4U / 5950X), not the old VM
        # skeleton. See hosts/nixos/minas-tirith/disko.nix before any disk work:
        # nine of its ten drives are live ZFS pool members.
        minas-tirith = mkNixos {
          system = "x86_64-linux";
          modules = [ ./hosts/nixos/minas-tirith ];
        };

        # GMKtec NucBox G10 Frigate host. The internal AirDisk NVMe is the sole
        # install target; its external Sabrent recording SSD is mount-only.
        osgiliath = mkNixos {
          system = "x86_64-linux";
          modules = [ ./hosts/nixos/osgiliath ];
        };

        # Raspberry Pi 5. Built through nixos-raspberrypi's OWN `nixosSystem`
        # wrapper (see lib/mkHost.nix `builder`): its board modules only
        # evaluate with the overlays that wrapper injects, and it sets
        # `hostPlatform` itself, so no `system` is passed here.
        #
        # Consequence, stated plainly: this host's package set comes from the
        # framework's nixpkgs pin, NOT this flake's 26.05. That is the only
        # combination upstream tests and binary-caches, and matches what the
        # NixOS-on-Pi-5 wiki independently recommends. pelargir is a
        # single-purpose appliance, so the divergence is contained here.
        pelargir = mkNixos {
          builder = inputs.nixos-raspberrypi.lib.nixosSystem;
          modules = [ ./hosts/nixos/pelargir ];
        };

        # Remaining unported hosts (builder-vm, minas-tirith-vm, osgiliath-vm,
        # pelargir-vm) are deliberately not wired up. Their sources are
        # still in hosts/nixos/, and the last state where they were declared
        # is on the `legacy/24.11` branch. Revive them one at a time by
        # porting to 26.05, rather than dragging eight broken hosts forward.
      };

      darwinConfigurations = {
        dol-amroth = nix-darwin.lib.darwinSystem {
          system = "aarch64-darwin";
          modules = [
            inputs.home-manager.darwinModules.home-manager
            ./hosts/darwin/dol-amroth
          ];
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

        # Pelargir has one disposable install target, but the short nvme name is
        # still unsafe: PCI discovery order can change across EEPROM/kernel
        # updates. Keep the serial-qualified device mechanically pinned.
        pelargir-disko-targets =
          let
            disks = nixosConfigurations.pelargir.config.disko.devices.disk;
            devices = lib.mapAttrsToList (_: d: d.device) disks;
            expected = [ "/dev/disk/by-id/nvme-KINGSTON_SNVS1000G_50026B7685D2B59A" ];
            zpools = nixosConfigurations.pelargir.config.disko.devices.zpool or { };
          in
          if devices != expected then
            throw "pelargir disko would touch ${toString devices} — expected exactly ${toString expected}"
          else if zpools != { } then
            throw "pelargir declares disko.devices.zpool (${toString (builtins.attrNames zpools)}) — this host has no disko-managed ZFS pools"
          else
            devPkgs.runCommand "pelargir-disko-targets-ok" { } "touch $out";

        # Osgiliath's external Sabrent SSD contains existing Frigate recordings
        # and must remain outside disko. Only the serial-pinned internal NVMe is
        # an installation target, and this host has no disko-managed ZFS pool.
        osgiliath-disko-targets =
          let
            disks = nixosConfigurations.osgiliath.config.disko.devices.disk;
            devices = lib.mapAttrsToList (_: d: d.device) disks;
            expected = [ "/dev/disk/by-id/nvme-AirDisk_1TB_SSD_QEK975R001121P1129" ];
            zpools = nixosConfigurations.osgiliath.config.disko.devices.zpool or { };
          in
          if devices != expected then
            throw "osgiliath disko would touch ${toString devices} — expected exactly ${toString expected}"
          else if zpools != { } then
            throw "osgiliath declares disko.devices.zpool (${toString (builtins.attrNames zpools)}) — this host has no disko-managed ZFS pools"
          else
            devPkgs.runCommand "osgiliath-disko-targets-ok" { } "touch $out";
      };
    };
}
