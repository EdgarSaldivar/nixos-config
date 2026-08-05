# pelargir — Pi 5 GPU-firmware direct boot from NVMe.
{ inputs, ... }:
{
  boot = {
    # Verified at nixos-raspberrypi 67616c2: "kernel" is the recommended
    # generational GPU-firmware loader. Its system.build.installBootLoader
    # copies config/firmware and each generation's kernel, initrd, cmdline,
    # DTBs, and overlays to /boot/firmware on install and generation switches;
    # it is an activation bootloader installer, not a migration service.
    loader = {
      raspberry-pi.bootloader = "kernel";
      systemd-boot.enable = false;
    };

    # The Pi 5 base sets this same package with mkDefault, so a normal value is
    # sufficient (and remains owner-overridable). At 67616c2 this matched
    # vendor kernel/firmware bundle defaults to Linux 6.18.39.
    kernelPackages = inputs.nixos-raspberrypi.packages.aarch64-linux.linuxPackages_rpi5;

    # Verified at 67616c2: base already supplies nvme and pcie_brcmstb; only
    # clk-rp1 is missing. All three are listed anyway — this option MERGES, so
    # the redundancy is free, and NVMe is the sole boot path: if an upstream
    # bump ever drops one, the host cannot find its root filesystem in initrd
    # and the failure mode is an unbootable box, not a degraded one.
    # (Defensive listing requested in review 2026-08-03.)
    initrd.availableKernelModules = [
      "nvme"
      "pcie_brcmstb"
      "clk-rp1"
    ];

    # k3s cannot start without these (verified on hardware 2026-08-05: it died
    # with `failed to find memory cgroup (v2)` on every attempt).
    #
    # The Raspberry Pi kernel ships with the memory cgroup controller DISABLED
    # by default to save a few MB on small boards, unlike essentially every
    # other distro kernel. Every containerised workload manager on a Pi needs it
    # turned back on explicitly, and the failure mode is a fatal error at
    # startup rather than a warning. cgroup v2 is already the NixOS default, so
    # only the memory controller needs enabling.
    kernelParams = [
      "cgroup_enable=memory"
      "cgroup_memory=1"
    ];

    tmp.cleanOnBoot = true;
  };

  # Mainline remains possible by replacing kernelPackages, but would give up
  # the framework's matched vendor kernel/firmware pair, defconfig, and cache
  # identity and could require restoring Pi 5/RP1 initrd support manually.
  # Owner decision: use nixos-raspberrypi's cached vendor bundle.

  # No config.txt PCIe override is intentional. Raspberry Pi OS on this exact
  # Pi 5 + Argon V5 dual-M.2 board auto-detected NVMe without pciex1 dtparam
  # (verified live 2026-08-03). The ASM1182e is a Gen2 switch, so forcing Gen3
  # would add risk without increasing link speed.
}
