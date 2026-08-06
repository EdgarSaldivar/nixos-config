# osgiliath — native UEFI boot. Secure Boot is disabled on this machine.
{ ... }:
{
  boot.loader = {
    systemd-boot = {
      enable = true;
      configurationLimit = 10;
      editor = false;
    };
    efi = {
      canTouchEfiVariables = true;
      efiSysMountPoint = "/boot";
    };
  };

  boot.tmp.cleanOnBoot = true;
}
