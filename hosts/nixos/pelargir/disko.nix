# pelargir — destructive disk layout.
#
# The full serial-qualified path is the safety boundary. Never replace it with a
# discovery-order name: Pi EEPROM and PCIe changes can renumber NVMe devices.
{ ... }:
{
  disko.devices.disk.root = {
    device = "/dev/disk/by-id/nvme-KINGSTON_SNVS1000G_50026B7685D2B59A";
    type = "disk";
    content = {
      type = "gpt";
      partitions = {
        firmware = {
          priority = 1;
          # Verified at nixos-raspberrypi 67616c2: the loader writes to its
          # firmwarePath, /boot/firmware by default; it does not look up a disk
          # label. vfat is required by the Pi GPU firmware, while FIRMWARE is
          # retained as the rescue/runbook identity. Keep 1 GiB because the
          # "kernel" mode retains multiple kernel/initrd/DTB generations.
          size = "1G";
          type = "EF00";
          content = {
            type = "filesystem";
            format = "vfat";
            extraArgs = [
              "-n"
              "FIRMWARE"
            ];
            mountpoint = "/boot/firmware";
            mountOptions = [ "umask=0077" ];

            # nixos-raspberrypi 67616c2 owns population through NixOS's
            # system.build.installBootLoader activation path. nixos-install and
            # later generation switches refresh this mount; disko only creates
            # and mounts it, so there is no duplicate copier or migrator here.
            postMountHook = ''
              echo "pelargir: firmware population is owned by nixos-raspberrypi"
            '';
          };
        };

        root = {
          priority = 2;
          size = "100%";
          content = {
            type = "filesystem";
            format = "ext4";
            extraArgs = [
              "-L"
              "nixos"
            ];
            mountpoint = "/";
            mountOptions = [
              "noatime"
              "errors=remount-ro"
            ];
          };
        };
      };
    };
  };
}
