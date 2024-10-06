 {
  disko.devices = {
    disk = {
      my-disk = {
        device = "/dev/sda";
        type = "disk";
        content = {
          type = "gpt";
          partitions = {
            FIRMWARE = {
              type = "EF00";
              size = "512M";
              label = "FIRMWARE";
              content = {
                type = "filesystem";
                format = "vfat";
                mountpoint = null;
              };
            };
            ESP = {
              type = "EF00";
              size = "512M";
              label = "ESP";
              content = {
                type = "filesystem";
                format = "vfat";
                mountpoint = "/boot";
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
