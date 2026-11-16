# edoras — coming back without anyone on site.
#
# There is no BMC and nobody there who can help, so a hang or a panic has to
# end in a reboot, not in a box that sits there until someone notices. The rest
# of the recovery story is outside the OS: BIOS "power on after AC loss" and a
# smart plug, both on the install checklist (docs/runbooks/edoras/install.md).
{ ... }:
{
  # systemd pets the hardware watchdog; a hung kernel reboots itself. The
  # chipset drivers (sp5100_tco on AMD, iTCO_wdt on Intel) are autoloaded by
  # PCI ID, so none is listed by hand: naming the wrong vendor's would fail
  # systemd-modules-load on every boot.
  #
  # VERIFY AFTER FIRST BOOT, as minas's hardware-health.nix warns, because
  # without a bound driver systemd runs with no watchdog at all, silently:
  #   ls -l /dev/watchdog*
  #   wdctl
  systemd.settings.Manager = {
    RuntimeWatchdogSec = "30s";
    RebootWatchdogSec = "10min";
  };

  # Reboot ten seconds after a kernel panic instead of halting at it.
  boot.kernel.sysctl."kernel.panic" = 10;

  # Decoded machine-check records and SMART, so a failing part leaves evidence.
  hardware.rasdaemon.enable = true;
  services.smartd = {
    enable = true;
    autodetect = true;
  };

  # Weekly trim rather than the `discard` mount option; pairs with `nodiscard`
  # in ./disko.nix.
  services.fstrim = {
    enable = true;
    interval = "weekly";
  };
}
