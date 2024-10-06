# default.nix
{ stdenv, ... }:

stdenv.mkDerivation {
  name = "mount-and-copy";

  # Define your source files, if any
  src = ./.;

  buildInputs = [];

  phases = [ "unpackPhase" "buildPhase" ];

  buildPhase = ''
    # Assuming /dev/sdX1 is the device and /mnt/FIRMWARE is the mount point
    # Replace /dev/sdX1 with the actual device identifier
    sudo mkdir -p /mnt/FIRMWARE
    sudo mount /dev/sda1 /mnt/FIRMWARE

    # Copy files during build time
    sudo cp -r ${src}/firmware/* /mnt/FIRMWARE/

    # Unmount the partition after copying files
    sudo umount /mnt/FIRMWARE
  '';

  meta = {
    description = "Mount a partition and copy files during build time";
    license = stdenv.lib.licenses.mit;
    maintainers = with stdenv.lib.maintainers; [ ];
  };
}
