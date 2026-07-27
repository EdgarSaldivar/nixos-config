# nardol — Ryzen 9 5950X, 125GB RAM, RTX 4090, ASRock X570 Taichi.
# Primary: inference + microservices. Secondary: gaming (Wolf).
#
# Named for the beacon of Gondor; siblings would be amon-din, eilenach,
# erelas, min-rimmon, calenhad, halifirien.
{ ... }:
{
  imports = [
    ./disko.nix
    ./hardware-configuration.nix

    ../../../modules/nixos/common.nix
    ../../../modules/nixos/docker.nix
    ../../../modules/nixos/gaming.nix
    ../../../modules/nixos/nvidia.nix

    ../../../users/edgar
  ];

  networking.hostName = "nardol";
  networking.useNetworkd = true;
  networking.useDHCP = true;

  users.users.edgar.extraGroups = [ "docker" ];

  boot.loader.systemd-boot = {
    enable = true;
    configurationLimit = 10;
    editor = false;
  };
  boot.loader.efi.canTouchEfiVariables = true;
  boot.loader.efi.efiSysMountPoint = "/boot";

  # DECISION NEEDED BEFORE FIRST DEPLOY.
  #
  # users/edgar sets `mutableUsers = false` and no password, so edgar can log
  # in over SSH by key but cannot run sudo — there is no password to enter.
  # Pick one:
  #
  #   a) set users.users.edgar.hashedPasswordFile from sops (preferred), or
  #   b) uncomment the line below to drop the sudo password requirement.
  #
  # security.sudo.wheelNeedsPassword = false;

  # Never change this after the first build; it pins state-migration
  # behaviour, it is not a version to keep current.
  system.stateVersion = "26.05";
}
