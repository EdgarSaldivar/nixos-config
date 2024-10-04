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
    nixos-hardware.url = "github:NixOS/nixos-hardware";
  };

  outputs = inputs@{ self, nixpkgs, home-manager, disko, sops, nixos-hardware, ... }: {
    nixosConfigurations = {
      pelargir = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [ ./hosts/pelargir inputs.disko.nixosModules.disko home-manager.nixosModules.home-manager sops.nixosModules.sops];
      };
      pelargir-vm = nixpkgs.lib.nixosSystem {
        system = "aarch64-linux";
        modules = [ ./hosts/pelargir-vm inputs.disko.nixosModules.disko home-manager.nixosModules.home-manager sops.nixosModules.sops];
      };
      minas-tirith = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [ ./hosts/minas-tirith inputs.disko.nixosModules.disko home-manager.nixosModules.home-manager sops.nixosModules.sops];
      };
      minas-tirith-vm = nixpkgs.lib.nixosSystem {
        system = "aarch64-linux";
        modules = [ ./hosts/minas-tirith-vm inputs.disko.nixosModules.disko home-manager.nixosModules.home-manager sops.nixosModules.sops];
      };
      osgiliath = nixpkgs.lib.nixosSystem {
        system = "aarch64-linux";
        modules = [ ./hosts/osgiliath inputs.disko.nixosModules.disko home-manager.nixosModules.home-manager sops.nixosModules.sops (import "${nixos-hardware}/raspberry-pi/4") ];
        specialArgs = { inherit nixpkgs; };
      };
    };
  };
}
