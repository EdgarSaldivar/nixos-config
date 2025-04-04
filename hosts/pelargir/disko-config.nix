{ pkgs, lib, config, disko, ... }:
let
  configTxt = pkgs.writeText "config.txt" ''
    [pi4]
    kernel=u-boot-rpi4.bin
    enable_gic=1

    # Otherwise the resolution will be weird in most cases, compared to
    # what the pi3 firmware does by default.
    disable_overscan=1

    # Supported in newer board revisions
    arm_boost=1

    [cm4]
    # Enable host mode on the 2711 built-in XHCI USB controller.
    # This line should be removed if the legacy DWC2 controller is required
    # (e.g. for USB device mode) or if USB support is not required.
    otg_mode=1

    [all]
    # Boot in 64-bit mode.
    arm_64bit=1

    # U-Boot needs this to work, regardless of whether UART is actually used or not.
    # Look in arch/arm/mach-bcm283x/Kconfig in the U-Boot tree to see if this is still
    # a requirement in the future.
    enable_uart=1

    # Prevent the firmware from smashing the framebuffer setup done by the mainline kernel
    # when attempting to show low-voltage or overtemperature warnings.
    avoid_warnings=1
  '';
in
{
  boot.postBootCommands = ''
    # On the first boot, resize the disk
    if [ -f /disko-first-boot ]; then
      set -euo pipefail
      set -x
      # Figure out device names for the boot device and root filesystem.
      rootPart=$(${pkgs.util-linux}/bin/findmnt -n -o SOURCE /)
      bootDevice=$(lsblk -npo PKNAME $rootPart)
      partNum=$(lsblk -npo MAJ:MIN $rootPart | ${pkgs.gawk}/bin/awk -F: '{print $2}')

      # Resize the root partition and the filesystem to fit the disk
      echo ",+," | sfdisk -N$partNum --no-reread $bootDevice
      ${pkgs.parted}/bin/partprobe
      ${pkgs.bcachefs-tools}/bin/bcachefs device resize $rootPart

      # Prevents this from running on later boots.
      rm -f /disko-first-boot
    fi
  '';
  disko = {
    imageBuilder.enableBinfmt = true;
    memSize = 6144;
    imageBuilder.qemu = (import pkgs.path { system = "x86_64-linux"; }).qemu + "/bin/qemu-system-aarch64 -M virt -cpu cortex-a57";
    imageBuilder.kernelPackages = pkgs.linuxPackages_latest;
    imageBuilder.extraPostVM = ''
      ${pkgs.zstd}/bin/zstd --compress $out/*raw
      rm $out/*raw
    '';
    devices = {
      disk = {
        disk1 = {
          imageSize = "20G";
          type = "disk";
          device = "/dev/mmcblk0";
          postCreateHook = ''
            lsblk
            sgdisk -A 1:set:2 /dev/vda
          '';
          content = {
            type = "gpt";
            partitions = {
              firmware = {
                size = "30M";
                priority = 1;
                type = "0700";
                content = {
                  type = "filesystem";
                  format = "vfat";
                  mountpoint = "/firmware";
                  ##!/usr/bin/env bash
                  postMountHook = toString (pkgs.writeScript "postMountHook.sh" ''
                    #!/usr/bin/env bash
                    set -euo pipefail
                    (cd ${pkgs.raspberrypifw}/share/raspberrypi/boot && cp bootcode.bin fixup*.dat start*.elf *.dtb /mnt/firmware/)
                    cp ${pkgs.ubootRaspberryPi4_64bit}/u-boot.bin /mnt/firmware/u-boot-rpi4.bin
                    cp ${configTxt} /mnt/firmware/config.txt
                  '');
                };
              };
              boot = {
                size = "1G";
                type = "EF00";
                content = {
                  type = "filesystem";
                  format = "vfat";
                  mountpoint = "/boot";
                };
              };
              root = {
                name = "root";
                size = "100%";
                content = {
                  type = "filesystem";
                  extraArgs = [ "--compression=zstd" ];
                  format = lib.mkOverride 5 "btrfs";
                  mountpoint = "/";
                  postMountHook = toString (pkgs.writeScript "postMountHook.sh" ''
                    #!${pkgs.bash}/bin/bash
                    touch /mnt/disko-first-boot
                  '');
                };
              };
            };
          };
        };
      };
    };
  };
}
