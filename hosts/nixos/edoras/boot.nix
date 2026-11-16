# edoras — UEFI boot through systemd-boot. Secure Boot must be OFF in the
# firmware (systemd-boot is unsigned); the on-site checklist in
# docs/runbooks/edoras/install.md turns it off.
{ ... }:
{
  boot.loader = {
    systemd-boot = {
      enable = true;
      configurationLimit = 10;
      editor = false;
    };
    efi = {
      # ⛔ FALSE ON PURPOSE. The install runs on nardol, whose NVRAM is the one
      # efibootmgr would write. Without an NVRAM entry the firmware falls back
      # to \EFI\BOOT\BOOTX64.EFI, which bootctl installs as well, so edoras
      # boots from its own disk with no firmware setup beyond the boot order.
      canTouchEfiVariables = false;
      efiSysMountPoint = "/boot";
    };
  };

  boot.tmp.cleanOnBoot = true;
}
