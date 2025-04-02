{ pkgs, lib, config, ... }: {
 environment.systemPackages = with pkgs; [
  git
  wget
  glances
  ];
 services.openssh.enable = true;
 # apparently it isnt enought to simply place the keys one must specify
 services.openssh.hostKeys = [
    { path = "/etc/ssh/ssh_host_ed25519_key"; type = "ed25519"; }
  ];

 networking.hostName = "pelargir";
 networking.useDHCP = true;
 networking.hostId = "54487bae"; #head -c 8 /etc/machine-id
 #networking.useDHCP = false;
 #networking.interfaces.ens18.useDHCP = true;
 #networking.interfaces.ens18.ipv4.addresses= "192.168.6.167"
 #networking.interfaces.enp0s31f6.useDHCP = true;
nixpkgs.config.allowUnfree = true;
# Set the time zone.
time.timeZone = "America/Los_Angeles";
system.stateVersion = "24.05";
security.sudo = {
  enable = true;
  wheelNeedsPassword = false;
};

sdImage = {
  populateRootCommands = ''
    mkdir -p ./files/boot/firmware
    ${pkgs.raspberrypifw}/share/raspberrypi/boot/*.bin ./files/boot/firmware
    ${pkgs.raspberrypifw}/share/raspberrypi/boot/*.dat ./files/boot/firmware
    ${pkgs.raspberrypifw}/share/raspberrypi/boot/*.elf ./files/boot/firmware
    echo "arm_64bit=1" > ./files/boot/firmware/config.txt
  '';
  compressImage = false;
  imageName = "nixos-sd-image-${config.system.nixos.version}-${pkgs.stdenv.hostPlatform.system}-pelargir.img";
};
}
