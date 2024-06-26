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
  };

  outputs = inputs@{ self, nixpkgs, home-manager, disko, ... }: {
    nixosConfigurations = {
      pelargir = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [ ./hosts/pelargir inputs.disko.nixosModules.disko home-manager.nixosModules.home-manager ];
      };
      minas-tirith = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [ ./hosts/minas-tirith inputs.disko.nixosModules.disko home-manager.nixosModules.home-manager ];
      };
    };
  };
}
