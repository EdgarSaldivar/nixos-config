# nardol — Ryzen 9 5950X, 125GB RAM, RTX 4090, ASRock X570 Taichi.
# Primary for now: headless game streaming. Future: k3s/server workloads.
#
# Named for the beacon of Gondor; siblings would be amon-din, eilenach,
# erelas, min-rimmon, calenhad, halifirien.
{ ... }:
{
  imports = [
    ./disko.nix
    ./hardware-configuration.nix
    ./boot.nix
    ./wolf.nix

    ../../../modules/nixos/docker.nix
    ../../../modules/nixos/gaming.nix
    ../../../modules/nixos/nvidia.nix

    ../../../users/edgar
  ];

  networking.hostName = "nardol";
  networking.useNetworkd = true;
  networking.useDHCP = true;

  # Match Triforce and Wolf's existing UID/GID contract so selectively restored
  # saves remain writable and future persistent service data never depends on
  # allocator order.
  users.groups.edgar.gid = 1000;
  users.users.edgar = {
    uid = 1000;
    group = "edgar";
    extraGroups = [
      "docker"
      "uinput"
    ];
  };

  boot.loader.systemd-boot = {
    enable = true;
    configurationLimit = 10;
    editor = false;
  };
  boot.loader.efi.canTouchEfiVariables = true;
  boot.loader.efi.efiSysMountPoint = "/boot";

  # (decision made — see below)
  #
  # users/edgar sets `mutableUsers = false` and no password, so edgar can log
  # in over SSH by key but cannot run sudo — there is no password to enter.
  # Pick one:
  #
  #   a) set users.users.edgar.hashedPasswordFile from sops (preferred), or
  #   b) uncomment the line below to drop the sudo password requirement.
  #
  # RESOLVED 2026-07-30, option (b). nardol has no sops secret, so edgar had no
  # password and `wheelNeedsPassword = true` meant sudo was IMPOSSIBLE — the host
  # was exported in a state where its only user could log in and then do nothing.
  # This matches minas-tirith, where access is key-only and sudo is passwordless.
  # If nardol ever gets a sops password, prefer option (a) and drop this.
  security.sudo.wheelNeedsPassword = false;

  # Never change this after the first build; it pins state-migration
  # behaviour, it is not a version to keep current.
  system.stateVersion = "26.05";
}
