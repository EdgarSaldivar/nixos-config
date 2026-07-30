# minas-tirith — the media/storage host at the remote site (formerly raz-server).
#
# Rebuilt 2026-07-30 from the old QEMU-VM skeleton onto real hardware, after the
# openSUSE btrfs root failed unrecoverably (`parent transid verify failed` on
# both DUP metadata copies — lost writes from 100 unclean shutdowns on a
# consumer NVMe with no power-loss protection).
#
# ⚠️  READ ./disko.nix BEFORE TOUCHING ANYTHING DISK-RELATED.
#     Nine of this machine's ten drives are live ZFS pool members holding ~98 TB,
#     all behind one Adaptec SAS HBA. Only the NVMe may ever be partitioned.
{ ... }:
{
  imports = [
    ./hardware-configuration.nix
    ./disko.nix
    ./boot.nix
    ./system.nix
    ./zfs.nix
    ./containers.nix
    ./monitoring.nix
    ./secrets.nix
    ../../../users/edgar/default.nix
  ];
}
