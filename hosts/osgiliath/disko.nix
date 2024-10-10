 {
  disko.devices = {
    disk = {
      my-disk = {
        device = "/dev/mmcblk1";
        type = "disk";
        content = {
          type = "gpt";
          partitions = {
            BOOT = {
              type = "EF00";
              size = "512M";
              label = "BOOT";
              content = {
                type = "filesystem";
                format = "vfat";
                #mountpoint = "/boot";
                mountOptions = [ "defaults" ];
              };
            };
            ESP = {
              type = "EF00";
              size = "512M";
              content = {
                type = "filesystem";
                format = "vfat";
                mountpoint = "/boot/efi";
              };
            };
            luks = {
              size = "100%";
              content = {
                type = "luks";
                name = "crypted";
                extraOpenArgs = [ "--allow-discards" ];
                #passwordphrase = "123";
                passwordFile = "/tmp/secret.txt";
                content = {
                  type = "filesystem";
                  format = "ext4";
                  mountpoint = "/";
                };
              };
            };
          };
        };
      };
    };
  };
}
