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
  xclip
  qemu  
  qemu-user
  ];
 services.openssh.enable = true;
 # apparently it isnt enought to simply place the keys one must specify
 services.openssh.hostKeys = [
    { path = "/etc/ssh/ssh_host_ed25519_key"; type = "ed25519"; }
  ];
 hardware.bluetooth.enable = false;

 networking.hostName = "builder-vm";
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
environment.etc."nix/nix.conf".text = lib.mkForce ''
    allowed-users = *
    auto-optimise-store = false
    builders =
    cores = 0
    max-jobs = auto
    require-sigs = true
    sandbox = true
    sandbox-fallback = false
    substituters = https://cache.nixos.org/
    system-features = nixos-test benchmark big-parallel kvm
    trusted-public-keys = cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY=
    trusted-substituters =
    trusted-users = root edgar
    extra-sandbox-paths =
    build-users-group = nixbld
    extra-experimental-features = nix-command flakes
  '';
nix = {
    settings = {
      builders = "local aarch64-linux";
      allowed-users = "*";
    };
  };
boot.binfmt.emulatedSystems = [ "aarch64-linux" ];
boot.binfmt.registrations.aarch64-linux.fixBinary = true;
/*
system.activationScripts.retrieve-age = {
    text = ''
      ${pkgs.bash}/bin/bash ./scripts/retrieve-age.sh
    '';
  };
*/
}
