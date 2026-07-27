{
  description = "Edgar's NixOS and nix-darwin configurations";

  inputs = {
    # Current release. New and ported hosts track this.
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

    # Darwin needs the nixpkgs-*-darwin branch, NOT nixos-*. Mixing the two
    # is what previously broke the Linux hosts: every host was pointed at
    # nixpkgs-24.11-darwin to make dol-amroth work.
    nixpkgs-darwin.url = "github:NixOS/nixpkgs/nixpkgs-24.11-darwin";

    nix-darwin = {
      url = "github:nix-darwin/nix-darwin/nix-darwin-24.11";
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
    inputs@{ nix-darwin, ... }:
    let
      inherit (import ./lib/mkHost.nix { inherit inputs; }) mkNixos;
    in
    {
      nixosConfigurations = {
        nardol = mkNixos {
          modules = [ ./hosts/nixos/nardol ];
        };

        # Unported hosts (builder-vm, minas-tirith[-vm], osgiliath[-vm],
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
    };
}
