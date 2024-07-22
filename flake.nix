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
  };

  outputs = inputs@{ self, nixpkgs, home-manager, disko, sops, ... }: {
    nixosConfigurations = {
      pelargir = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [ ./hosts/pelargir inputs.disko.nixosModules.disko home-manager.nixosModules.home-manager sops.nixosModules.sops];
      };
      minas-tirith = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [ ./hosts/minas-tirith inputs.disko.nixosModules.disko home-manager.nixosModules.home-manager sops.nixosModules.sops];
      };
    };
  };
}
