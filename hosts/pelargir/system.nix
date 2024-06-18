{ pkgs, config, ... }: {
 environment.systemPackages = with pkgs; [
  zfs
  git
  ];
 services.openssh.enable = true;
 hardware.bluetooth.enable = false;
}
