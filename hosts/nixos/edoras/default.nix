# edoras — a friend's PC at his house, run entirely by the fleet owner: RTX 3090
# + RTX A4000. Planned role: the fleet's always-on inference host (Pinstellation
# readers and recognition, a fleet chat endpoint, the Dungeon Scriber worker).
#
# This is the foundation only: identity, network, remote recovery and the
# NVIDIA driver. No workload runs here yet; each one arrives in its own change.
#
# ⛔ NOBODY ON SITE CAN HELP. The owner of the hardware is not technical and has
# no access, and there is no BMC. Every choice below assumes the box must come
# back by itself: no disk encryption (nothing could answer a passphrase
# prompt), a watchdog, reboot on panic, and BIOS power-on after AC loss. See
# docs/runbooks/edoras/install.md for the on-site checklist.
#
# Not a k3s node: an endpoint needs no scheduling, and an agent at a third site
# would make pelargir, the only control plane, reach into someone else's house.
{ ... }:
{
  imports = [
    ./hardware-configuration.nix
    ./disko.nix
    ./boot.nix
    ./system.nix
    ./recovery.nix
    ./secrets.nix
    ../../../modules/nixos/fleet/nvidia-headless.nix
    ../../../users/edgar
  ];

  # Inference only, no games: the 32-bit graphics userspace stays off.
  fleet.nvidiaHeadless.enable = true;
}
