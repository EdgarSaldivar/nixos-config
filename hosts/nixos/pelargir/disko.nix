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
          # raspberry-pi-nix 3e8100d's image module defaults to 128 MiB, but its
          # migrator-selected firmware/DTB/overlay set already measures about
          # 25 MiB before kernel + initrd. Retain 1 GiB for update headroom;
          # label and mountpoint are the module's actual lookup contract.
          size = "1G";
          type = "EF00";
          content = {
            type = "filesystem";
            format = "vfat";
            extraArgs = [ "-n" "FIRMWARE" ];
            mountpoint = "/boot/firmware";
            mountOptions = [ "umask=0077" ];

            # raspberry-pi-nix owns firmware population and later in-place
            # refreshes. Do not duplicate its kernel/config/firmware copier in
            # disko; INSTALL-RUNBOOK invokes the pinned rev's migration program
            # once after nixos-install, before the first boot can run its unit.
            postMountHook = ''
              echo "pelargir: firmware population is owned by raspberry-pi-nix"
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
