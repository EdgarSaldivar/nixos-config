{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.05";
    home-manager = {
      url = "github:nix-community/home-manager/master";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    disko = {
      url = "github:nix-community/disko";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    sops = {
      url = "github:Mic92/sops-nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.nixpkgs-stable.follows = "";
    };
    fluxcd.url = "github:fluxcd/flux2";
  };

  outputs = inputs@{ self, nixpkgs, home-manager, disko, sops, ... }: {
    nixosConfigurations = {
      pelargir = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [ ./hosts/pelargir inputs.fluxcd.nixosModules.fluxcd inputs.disko.nixosModules.disko home-manager.nixosModules.home-manager sops.nixosModules.sops];
      };
      pelargir-vm = nixpkgs.lib.nixosSystem {
        system = "aarch64-linux";
        modules = [ ./hosts/pelargir-vm inputs.fluxcd.nixosModules.fluxcd inputs.disko.nixosModules.disko home-manager.nixosModules.home-manager sops.nixosModules.sops];
      };
      minas-tirith = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [ ./hosts/minas-tirith inputs.fluxcd.nixosModules.fluxcd inputs.disko.nixosModules.disko home-manager.nixosModules.home-manager sops.nixosModules.sops];
      };
    };
  };
}
