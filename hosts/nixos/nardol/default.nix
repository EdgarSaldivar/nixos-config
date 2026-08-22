# nardol — Ryzen 9 5950X, 125GB RAM, RTX 4090, ASRock X570 Taichi.
# Primary: headless game streaming. A future cluster role, if a concrete batch
# workload justifies it, is a tainted on-demand k3s agent. This intermittently
# powered host must not become a control-plane or datastore quorum member.
#
# Named for the beacon of Gondor; siblings would be amon-din, eilenach,
# erelas, min-rimmon, calenhad, halifirien.
{ pkgs, ... }:
{
  imports = [
    ./disko.nix
    ./hardware-configuration.nix
    ./boot.nix
    ./wolf.nix

    ../../../modules/nixos/roles/game-streaming-docker.nix
    ../../../modules/nixos/fleet/disk-health.nix
    ../../../modules/nixos/roles/game-streaming.nix
    ../../../modules/nixos/roles/nvidia-headless.nix

    ../../../users/edgar
  ];

  networking.hostName = "nardol";
  networking.useNetworkd = true;
  networking.useDHCP = true;

  # Disk telemetry is Nardol's only fleet role. This does not enable k3s; the
  # first activation requires one interactive `sudo tailscale up` login.
  fleet.diskHealth = {
    enable = true;
    hostId = "nardol";
  };
  # The I211 supports magic-packet wakeup, but firmware enablement alone does
  # not guarantee that the driver leaves it armed at shutdown. Match the same
  # immutable MAC used by the initrd instead of relying on a predictable name.
  systemd.network.links."10-nardol-i211-wake" = {
    matchConfig.MACAddress = "9c:6b:00:36:e0:e8";
    linkConfig = {
      WakeOnLan = "magic";

      # ⛔ Name is DECLARED here, not inherited by accident.
      #
      # nardol does not set net.ifnames=0, so predictable naming is on and udev
      # computes ID_NET_NAME_PATH=enp9s0 for this card. The interface is
      # nevertheless `eth0`, for a subtle reason: a matching .link file with no
      # NamePolicy means NO renaming, so the kernel name survives. This file was
      # written for WakeOnLan and was silently deciding the interface name as a
      # side effect.
      #
      # ⚠️ That matters because the VBAN firewall rule in wolf.nix matches
      # `-i eth0`. Adding a NamePolicy here, or deleting this link, renames the card
      # to enp9s0 and that rule stops matching -- silently: iptables accepts the
      # rule, it simply never fires, VBAN packets fall through to
      # nixos-fw-log-refuse, and the microphone just does not work.
      #
      # Cross-review flagged that rule as already broken on 2026-08-22. It is not --
      # verified on the live host, eth0 carries the expected MAC -- but it was
      # relying on an undeclared side effect. Now it is declared.
      Name = "eth0";
    };
  };
  environment.systemPackages = [ pkgs.ethtool ];

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
