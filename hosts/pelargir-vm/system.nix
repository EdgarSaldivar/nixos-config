{ pkgs, lib, config, ... }: {
 environment.systemPackages = with pkgs; [
  zfs
  lsof
  git
  sops
  age
  _1password
  ssh-to-age
  wget
  ];
 services.openssh.enable = true;
 # apparently it isnt enought to simply place the keys one must specify
 services.openssh.hostKeys = [
    { path = "/etc/ssh/ssh_host_ed25519_key"; type = "ed25519"; }
  ];
 hardware.bluetooth.enable = false;

 networking.hostName = "pelargir-vm";
 networking.useDHCP = true;
 #networking.hostId = "5b1c2f72"; #head -c 8 /etc/machine-id
 #networking.useDHCP = false;
 #networking.interfaces.ens18.useDHCP = true;
 #networking.interfaces.ens18.ipv4.addresses= "192.168.6.167"
 #networking.interfaces.enp0s31f6.useDHCP = true;
nixpkgs.config = {
  allowUnfree = true;
};
# Set the time zone.
time.timeZone = "America/Los_Angeles";
system.stateVersion = "24.11";
security.sudo = {
  enable = true;
  wheelNeedsPassword = false;
};

/*
system.activationScripts.retrieve-age = {
    text = ''
      ${pkgs.bash}/bin/bash ./scripts/retrieve-age.sh
    '';
  };
*/
}
