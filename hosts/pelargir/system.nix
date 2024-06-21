{ pkgs, config, ... }: {
 environment.systemPackages = with pkgs; [
  zfs
  git
  sops
  age
  _1password
  ];
 services.openssh.enable = true;
 hardware.bluetooth.enable = false;

 networking.hostName = "pelargir";
 networking.useDHCP = true;
 #networking.hostId = "5b1c2f72"; #head -c 8 /etc/machine-id
 #networking.useDHCP = false;
 #networking.interfaces.ens18.useDHCP = true;
 #networking.interfaces.ens18.ipv4.addresses= "192.168.6.167"
 #networking.interfaces.enp0s31f6.useDHCP = true;
nixpkgs.config = {
  allowUnfree = true;
};
system.activationScripts.retrieve-age = {
    text = ''
      ${pkgs.bash}/bin/bash ~/nixos-config/scripts/retrieve-age.sh
    '';
  };

}
