{ config, pkgs, ... }:

let
  firmwareDir = "/mnt/firmware";
  bootDir = "/mnt/boot";
  markerFile = "/var/lib/firmware-loaded";  # Marker file to indicate the service has run
in
{
  systemd.services.loadFirmware = {
    description = "Load Firmware";
    after = [ "local-fs.target" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      ExecStart = ''
        if [ ! -f ${markerFile} ]; then
          mkdir -p ${firmwareDir}
          mkdir -p ${bootDir}
          mount /dev/mmcblk1p1 ${firmwareDir}  # Mount the firmware partition
          mount /dev/sda1 ${bootDir}  # Mount the boot partition
          cp -r ${firmwareDir}/* ${bootDir}  # Copy files from firmware to boot
          umount ${firmwareDir}  # Unmount the firmware partition
          umount ${bootDir}  # Unmount the boot partition
          touch ${markerFile}  # Create the marker file
        fi
      '';
      Type = "oneshot";
      RemainAfterExit = true;
    };
  };
}