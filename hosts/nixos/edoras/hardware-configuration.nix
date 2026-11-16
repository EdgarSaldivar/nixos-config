# edoras — Gigabyte Z590 AORUS ULTRA hardware inventory, probed 2026-11-15
# (`nixos-generate-config --no-filesystems --show-hardware-config`, lspci,
# dmidecode) on first boot.
#
# CPU   : Intel Core i9-10850K (Comet Lake, 10C/20T). 10th gen: the board's
#         M2A_CPU socket is dead and its CPU slots run PCIe 3.0.
# Board : Gigabyte Z590 AORUS ULTRA, BIOS F2 (2021-01-26, the launch release)
# RAM   : 2 x 16 GiB G.Skill F4-3200C16-16GTZR at 3200 MT/s
# GPU 0 : RTX 3090 (Gigabyte, subsystem 1458:4041), GA102, 24 GiB GDDR6X,
#         01:00.0 in PCIEX16 (x16), default power limit 420 W, max 450 W,
#         UUID GPU-fe6604a2-e3a8-7eb0-2716-8fb9d8327cba
# GPU 1 : RTX A4000, GA104, 16 GiB GDDR6, 140 W, single-slot blower,
#         07:00.0 in PCIEX4 (chipset, x4) so the 3090 keeps x16 and an air gap,
#         UUID GPU-e26e87e1-860f-0a89-7ff2-bb91cd5027a3
#         Pin workloads by these UUIDs, never by index: PCI order can change.
# Root  : Intel 660p 1 TB NVMe, SSDPEKNW010T8, serial BTNH10350EER1P0B, in
#         M2M_SB (chipset). The only disko-managed device; see ./disko.nix.
#         QLC and DRAM-less: fine for read-mostly model weights, wrong for
#         write churn such as build caches. SMART 2026-11-15: 0% used, 3.29 TB
#         written of 200 TBW, no media errors.
# LAN   : Intel I225-V rev 03 (igc), 18:c0:4d:89:97:9b; see ./system.nix
# Wi-Fi : Intel AX200 (iwlwifi), present and deliberately unmanaged
# Watchdog: intel_oc_wdt binds /dev/watchdog0, 30 s (see ./recovery.nix)
#
# Burn-in 2026-11-15 (gpu-burn, both cards at once, four separate PSU cables):
#   3090 capped at 300 W: 71 C, fan 85 %.   A4000 140 W: 91 C (its 90 C target).
#   3090 at stock 420 W:  85 C, fan 96 %.   No thermal throttling, no Xid,
#   0 compute errors, the host stayed up. The 3090's GDDR6X temperature was
#   not visible then; see ./vram-temp.nix.
# GDDR6X and gpu-unlock, 2026-11-15 (gpu-burn -tc after the unlock): the memory
#   reached 104 C within 3 minutes at 420 W with the core at 80 C, so the card
#   now runs capped at 300 W (./power-cap.nix): 86-88 C, 67.2 TFLOP/s. Tensor
#   throughput at 420 W went 66.0 -> 83.4 TFLOP/s with the unlock; the A4000
#   control stayed at ~34.
{
  config,
  lib,
  modulesPath,
  ...
}:
{
  imports = [ (modulesPath + "/installer/scan/not-detected.nix") ];

  boot.initrd.availableKernelModules = [
    "xhci_pci"
    "ahci"
    "nvme"
    "usbhid"
  ];
  boot.initrd.kernelModules = [ ];
  boot.kernelModules = [ "kvm-intel" ];
  boot.extraModulePackages = [ ];

  hardware.enableRedistributableFirmware = true;
  hardware.cpu.intel.updateMicrocode = lib.mkDefault config.hardware.enableRedistributableFirmware;

  swapDevices = [ ];
  nixpkgs.hostPlatform = lib.mkDefault "x86_64-linux";
}
