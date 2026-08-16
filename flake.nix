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

      # The Mac this is usually driven from. `nix fmt` and local runs use it.
      devSystem = "aarch64-darwin";
      devPkgs = nixpkgs.legacyPackages.${devSystem};

      # ⛔ The check suite MUST be exposed for every system CI runs on.
      #
      # `nix flake check` only checks the CURRENT system's outputs. For most of
      # this repository's life the suite existed solely under aarch64-darwin, so
      # a Linux CI job would have requested `checks.x86_64-linux`, found nothing,
      # and reported green having run no invariant at all. That is worse than no
      # CI, because it manufactures confidence — and it is exactly the trap
      # ROADMAP warned about before this was fixed.
      #
      # The .github/workflows/flake-check.yml `parity` job asserts both systems
      # expose the SAME check names, so a contract cannot be added to one
      # platform and silently skipped on the other.
      checkSystems = [
        "aarch64-darwin"
        "x86_64-linux"
      ];
      forAllCheckSystems = lib.genAttrs checkSystems;
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
      # who remembered to run it. These checks encode a mistake that was
      # actually made here.
      checks = forAllCheckSystems (
        system:
        import ./checks {
          inherit lib nixosConfigurations darwinConfigurations;
          pkgs = nixpkgs.legacyPackages.${system};
        }
      );
    };
}
