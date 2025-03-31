{ lib, pkgs, ... }:

let
  firmwareFiles = [
    {
      url = "https://github.com/raspberrypi/firmware/raw/master/boot/bootcode.bin";
      sha256 = "placeholder-sha256";
    }
    {
      url = "https://github.com/raspberrypi/firmware/raw/master/boot/start.elf";
      sha256 = "placeholder-sha256";
    }
    {
      url = "https://github.com/raspberrypi/firmware/raw/master/boot/fixup.dat";
      sha256 = "placeholder-sha256";
    }
    # Add other necessary firmware files here
  ];

  fetchFirmware = file: pkgs.fetchurl {
    url = file.url;
    sha256 = file.sha256;
  };

  firmwareDerivation = pkgs.runCommand "raspberry-pi-firmware" { buildInputs = [ pkgs.wget ]; } ''
    mkdir -p $out/boot
    ${lib.concatMapStringsSep "\n" (file: ''
      wget ${file.url} -O $out/boot/$(basename ${file.url})
    '') firmwareFiles}
  '';
in
{
  environment.etc."firmware".source = firmwareDerivation;
}