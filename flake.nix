{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.05";
    disko.url = "github:nix-community/disko";
    disko.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = inputs@{ self, nixpkgs, ... }: {
    nixosConfigurations = {
      pelargir = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [ ./hosts/pelargir inputs.disko.nixosModules.disko ];
      };
      minas-tirith = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [ ./hosts/minas-tirith inputs.disko.nixosModules.disko ];
      };
      # add more hosts as needed
    };
  };
}
