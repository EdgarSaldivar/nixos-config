# minas-tirith (raz-server) — real hardware.
#
# Replaces the previous generated file, which imported profiles/qemu-guest.nix
# and declared only xhci_pci + sr_mod: that was a VM profile and would not have
# booted this board.
#
# Board : ASRock Rack X570D4U (UEFI, BIOS L1.59C)
# CPU   : AMD Ryzen 9 5950X (16c/32t)
# RAM   : 128 GB DDR4, non-ECC   <-- no ECC: silent memory errors are invisible
# Root  : Samsung 980 1TB NVMe @ 0000:23:00.0   (DRAM-less, no PLP)
# HBA   : Adaptec Series 7 6G SAS @ 0000:2e:00.0 (aacraid) -> all 9 pool disks
# NIC   : 2x Intel igb — enp38s0 a8:a1:59:c0:4e:73 (used), enp39s0 ...:72 (down)
# GPU   : NVIDIA RTX 2080 (Turing)
{
  config,
  lib,
  modulesPath,
  ...
}:
{
  imports = [ (modulesPath + "/installer/scan/not-detected.nix") ];

  # Root is LUKS-on-NVMe, and the initrd must also bring up the network for
  # remote LUKS unlock over SSH (see ./boot.nix) — hence `igb` here. Without
  # igb in the initrd there is no network, and therefore no way to unlock this
  # machine remotely.
  boot.initrd.availableKernelModules = [
    "nvme"
    "xhci_pci"
    "ahci"
    "usbhid"
    "usb_storage"
    "sd_mod"
    "igb"
  ];

  boot.initrd.kernelModules = [ ];

  # aacraid is deliberately NOT in the initrd. Root does not live on the HBA,
  # and the ZFS pools are imported later in userspace (see ./zfs.nix). Keeping
  # the HBA out of early boot also means an installer booted with
  # `modprobe.blacklist=aacraid` behaves identically to normal early boot.
  boot.kernelModules = [ "kvm-amd" ];
  boot.extraModulePackages = [ ];

  # Carried over from the openSUSE install. This board mis-assigns PCI BARs
  # without it; it is also why kernel disk names on this host must never be
  # trusted (always use /dev/disk/by-id).
  boot.kernelParams = [ "pci=realloc=off" ];

  hardware.enableRedistributableFirmware = true;
  hardware.cpu.amd.updateMicrocode = lib.mkDefault config.hardware.enableRedistributableFirmware;

  # No swap. The old box had a 2 GiB LUKS swap partition that was permanently
  # 0 B used with 128 GB of RAM and swappiness=10, and there is no hibernation.
  swapDevices = [ ];

  nixpkgs.hostPlatform = lib.mkDefault "x86_64-linux";
}
