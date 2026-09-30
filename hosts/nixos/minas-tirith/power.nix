# minas-tirith — CPU power policy.
#
# Measured 2026-09-29 with RAPL at ~99% idle: package 44 W, of which the cores were
# 1.2 W. The cores already sit in deep idle nearly all the time; the rest is the IO
# die, fabric and L3. So the OS lever here is narrow — how eagerly amd-pstate-epp
# ramps and boosts when work arrives — and the larger idle cost is wakeups (see the
# palworld manifest) and hardware.
#
# Deliberately NOT declared here because they were already on: PCIe ASPM L1 on every
# endpoint that supports it (BIOS; the Adaptec HBA does not), NVMe APST, and SATA
# link power management. `powertop --auto-tune` is deliberately not used: it enables
# USB autosuspend, which can drop BMC iKVM input — the fallback console on a machine
# an hour away.
{ ... }:
{
  # amd-pstate-epp runs in active mode (kernel default) under the powersave governor,
  # and firmware leaves EPP at balance_performance. balance_power trims frequency and
  # boost on short bursts. Boost itself stays ON: Plex scans and game servers are
  # single-thread bound, and the SoC floor would not move.
  #
  # tmpfiles `w` accepts globs, so one line covers every CPU. It runs at boot
  # (systemd-tmpfiles-setup) and again on every switch (systemd-tmpfiles-resetup via
  # sysinit-reactivation.target). Switching the governor to `performance` forces EPP
  # to performance and overrides this.
  systemd.tmpfiles.rules = [
    "w /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference - - - - balance_power"
  ];
}
