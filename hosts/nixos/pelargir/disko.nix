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
          size = "1G";
          type = "EF00";
          content = {
            type = "filesystem";
            format = "vfat";
            mountpoint = "/boot/firmware";
            mountOptions = [ "umask=0077" ];

            # nixos-hardware master added the missing D1 machinery in
            # raspberry-pi/common/firmware.nix: its activation installer copies
            # bcm2712 DTBs, overlays, the complete start/fixup set, generated
            # config.txt, and U-Boot. Keep that single implementation rather
            # than freezing a second copy in disko's postMountHook. It runs from
            # nixos-install activation and on every later rebuild; boot.nix opts
            # it in. INSTALL-RUNBOOK still gates reboot on inspecting the FAT.
            postMountHook = ''
              echo "pelargir: firmware population is owned by the nixos-hardware activation script"
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
