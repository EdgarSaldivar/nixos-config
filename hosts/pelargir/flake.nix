{
 inputs = {
  nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.05";
  disko.url = "github:nix-community/disko";
  disko.inputs.nixpkgs.follows = "nixpkgs";
 };

 outputs = inputs@{ self, nixpkgs, ... }: {
  nixosConfigurations.nixos = nixpkgs.lib.nixosSystem {
  system = "x86_64-linux";
  modules = [ ./configuration.nix inputs.disko.nixosModules.disko ];
  };
 };
}
