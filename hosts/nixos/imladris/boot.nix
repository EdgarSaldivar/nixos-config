# imladris — Pi 5 GPU-firmware boot from microSD.
{ inputs, lib, ... }:
{
  boot = {
    # Same generational GPU-firmware loader pelargir uses. Its
    # system.build.installBootLoader copies config/firmware and each
    # generation's kernel, initrd, cmdline, DTBs and overlays to /boot/firmware
    # on install and on every generation switch.
    loader = {
      raspberry-pi.bootloader = "kernel";
      systemd-boot.enable = false;
    };

    kernelPackages = inputs.nixos-raspberrypi.packages.aarch64-linux.linuxPackages_rpi5;

    # ⚠️ DIFFERENT FROM PELARGIR ON PURPOSE. pelargir boots from NVMe over the
    # PCIe connector and lists nvme/pcie_brcmstb/clk-rp1. This host boots from
    # the microSD, so the SD host controller is the load-bearing driver and the
    # PCIe connector is deliberately left free (see ./system.nix).
    #
    # These four are the drivers actually bound on Pi 5 hardware, read off the
    # running pelargir 2026-09-11 rather than guessed:
    #   sdhci-brcmstb 1000fff000.mmc: Got CD GPIO
    #   mmc0: SDHCI controller on 1000fff000.mmc using ADMA 64-bit
    #   mmc0: new UHS-I speed SDR104 SDHC card
    # `sdhci_of_dwcmshc` is the WRONG guess for this board; the Pi 5 uses the
    # Broadcom STB controller. Listed defensively — the option merges, so
    # overlap with the Pi 5 base costs nothing, and a missing one here is an
    # unbootable box rather than a degraded one.
    initrd.availableKernelModules = [
      "mmc_block"
      "sdhci"
      "sdhci_pltfm"
      "sdhci_brcmstb"
    ];

    # Imladris's LAN now arrives through a Realtek USB Ethernet adapter. The
    # root filesystem has no network unlock path, so this need not be in the
    # initrd; loading it at boot makes the stage-2 DHCP interface deterministic.
    kernelModules = [ "r8152" ];

    # ⛔ NOT cargo-culted from pelargir's k3s requirement. This host runs no
    # container runtime at all. The memory controller is enabled because the
    # Raspberry Pi kernel ships it DISABLED by default, and without it systemd's
    # MemoryMax/MemoryHigh silently do nothing — which would quietly void the
    # resource limits ./media.nix puts on Jellyfin. A limit that is accepted and
    # never enforced is worse than no limit, because it reads as protection.
    kernelParams = [
      "cgroup_enable=memory"
      "cgroup_memory=1"
    ];

    tmp.cleanOnBoot = true;
  };

  # ---------------------------------------------------------------------------
  # Reject ALL LVM physical-volume scanning.
  # ---------------------------------------------------------------------------
  # ⛔ This is not defensive hygiene; it fixes something observed on this exact
  # hardware. Bay 0:0 is nardol's old Crucial P3, which still carries a foreign
  # Proxmox "pve" volume group. When the enclosure was attached to pelargir on
  # 2026-09-10, that host activated roughly twenty of its thin volumes and
  # snapshots unprompted — pve/root, pve/data, base-101/105/116 templates, and a
  # pile of vm-111/114/122 snapshots — because pelargir has no such filter.
  #
  # Nothing was mounted and nothing was damaged, but device-mapper nodes for a
  # stranger's virtual machines appearing on an appliance is not a state worth
  # tolerating, and bay 0:0 is about to be repartitioned by hand. Stale
  # signatures that activate themselves are precisely what turns a careful
  # format into an accident.
  #
  # nardol/boot.nix carries this same filter for this same drive; that is where
  # the pattern comes from. There is no LUKS here, so disabling services.lvm
  # outright was considered and rejected: its device-mapper udev rules are cheap,
  # and a filter that rejects everything is easier to reason about than a service
  # that is sometimes absent.
  #
  # Stage 2 ONLY, deliberately. pelargir's activation happened through udev after
  # switch-root, which is this file. No initrd counterpart is declared because
  # root is plain ext4 on the microSD and stage 1 never runs LVM activation here
  # — and `boot.initrd.systemd.contents` would be silently inert anyway, since
  # this host does not enable the systemd initrd (nardol does, for Clevis).
  environment.etc."lvm/lvm.conf".text = lib.mkAfter ''
    devices/global_filter = [ "r|.*|" ]
  '';
}
