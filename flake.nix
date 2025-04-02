{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    disko = {
      url = "github:nix-community/disko/latest";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    sops = {
      url = "github:Mic92/sops-nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.nixpkgs-stable.follows = "";
    };
    nixos-hardware.url = "github:NixOS/nixos-hardware";
    sdImage.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = inputs@{ self, nixpkgs, home-manager, disko, sops, nixos-hardware, sdImage, ... }: {
    nixosConfigurations = {
      pelargir = nixpkgs.lib.nixosSystem {
        system = "aarch64-linux";
        modules = [ ./hosts/pelargir inputs.disko.nixosModules.disko home-manager.nixosModules.home-manager sops.nixosModules.sops (import "${nixos-hardware}/raspberry-pi/4") (import "${nixpkgs}/nixos/modules/installer/sd-card/sd-image.nix") ];
        #specialArgs = { inherit nixpkgs; };
      };
      pelargir-vm = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
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
        modules = [ ./hosts/osgiliath inputs.disko.nixosModules.disko home-manager.nixosModules.home-manager sops.nixosModules.sops (import "${nixos-hardware}/raspberry-pi/4") (import "${nixpkgs}/nixos/modules/installer/sd-card/sd-image.nix") ];
        #specialArgs = { inherit nixpkgs; };
      };
      osgiliath-vm = nixpkgs.lib.nixosSystem {
        system = "aarch64-linux";
        modules = [ ./hosts/osgiliath-vm inputs.disko.nixosModules.disko home-manager.nixosModules.home-manager sops.nixosModules.sops (import "${nixos-hardware}/raspberry-pi/4") ];
        #specialArgs = { inherit nixpkgs; };
      };
    };
  };
}