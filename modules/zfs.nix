{
  description = "NixOS configuration for ZFS pool import, doesn not create pool.";

  outputs = { self, nixpkgs, nixos-hardware }: {
    nixosConfigurations = {
      my-server = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [
          nixos-hardware.nixosModules.common
          {
            imports = [ ./hardware-configuration.nix ];

            boot.supportedFilesystems = [ "zfs" ];

            services.zfs = {
              enable = true;
              autoScrub = false;
              autoSnapshot.enable = false;
              autoImport = true; 
            };

            fileSystems."/storage" = {
              device = "rpool/secondary";
              fsType = "zfs";
            };
          }
        ];
      };
    };
  };
}