 { lib, ... }:
 {
  disko.devices = {
    disk = {
      my-disk = {
        device = "/dev/sda";
        type = "disk";
        content = {
          type = "gpt";
          partitions = {
            ESP = {
              type = "EF02";
              size = "512M";
              content = {
                type = "filesystem";
                format = "vfat";
                mountpoint = "/boot";
              };
            };
            root = {
              size = "100%";
              content = {
                #type = "luks";
                type = "filesystem";
                #name = "NIXOS_SD";
                #extraOpenArgs = [ "--allow-discards" ];
                #passwordphrase = "123";
                #passwordFile = "/tmp/secret.txt";
                format = "ext4";
                mountpoint = lib.mkForce  "/";
              };
            };
          };
        };
      };
    };
  };
}
