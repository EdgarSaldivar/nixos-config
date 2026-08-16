# minas-tirith — identity, access, Nix policy and the host package set.
#
# Split out of a 1,361-line system.nix on 2026-08-16. The seam is unit lifecycle,
# not line count: everything here is small, static host policy that changes for
# unrelated reasons. Networking, hardware health and the backup program each own
# their own file now.
{ config, pkgs, ... }:
let
  ingressAcceptance = pkgs.callPackage ./scripts/package.nix { };
in
{
  # ---------------------------------------------------------------------------
  # Identity
  # ---------------------------------------------------------------------------
  # Renamed from raz-server -> minas-tirith as of the NixOS rebuild. Verified
  # safe: nothing in the five docker-compose stacks references the old name (only
  # /etc/hostname and a historical git author string did), and the
  # host-hostnames helper (dvdarias/docker-hoster) only writes *container* names
  # into /etc/hosts. The public DNS name minas.saldivar.io already matches.
  #
  # The ZFS pool labels still record hostname 'raz-server'; they will record this
  # name after the clean export/import handover described in ./zfs.nix.
  networking.hostName = "minas-tirith";
  networking.domain = "saldivar.io";

  # Required for ZFS. The value itself is arbitrary as long as it never changes
  # — after the clean export/import handover (see ./zfs.nix) the pools record
  # whatever this host presents. This is the value the old install used, kept
  # for continuity.
  networking.hostId = "0149f5f3";

  system.stateVersion = "26.05";

  # ---------------------------------------------------------------------------
  # Access
  # ---------------------------------------------------------------------------
  services.openssh = {
    # EXPLICIT, not relying on the default. The only external route to this
    # machine is a NAT forward `minas.saldivar.io:2222 -> 10.0.1.6:22`, owned by
    # someone else's router. If this port ever silently changed, remote access
    # would vanish with no way to fix it remotely. It also has to match the
    # initrd sshd port (see ./boot.nix), which shares 22 so that the same single
    # forward reaches both the unlock prompt and the booted system.
    ports = [ 22 ];
    settings = {
      # The old box was taking SSH brute-force on an internet-facing forward
      # with password auth enabled. Not repeating that.
      KbdInteractiveAuthentication = false;
    };
  };

  # NOTE: restore the old SSH host keys from the backup after install if you
  # want existing known_hosts entries and automation to keep working:
  #   /storage2/backup-2026-07-30/etc/ssh/ssh_host_*
  # Otherwise every client sees a host key change on first connect.

  security.sudo = {
    enable = true;
    wheelNeedsPassword = false;
  };

  # Console login password is provided via sops — see ./secrets.nix.
  # (mutableUsers = false + key-only sshd would otherwise leave the SOL console
  # showing a login prompt nobody can satisfy.)

  # ---------------------------------------------------------------------------
  # Nix itself
  # ---------------------------------------------------------------------------
  # Intentional host-specific complement to the baseline's scheduled optimise:
  # compact duplicate store paths during writes on this storage-constrained host.
  nix.settings.auto-optimise-store = true;

  environment.systemPackages = with pkgs; [
    # storage / diagnostics
    zfs
    smartmontools
    nvme-cli
    usbutils
    # BMC — ipmitool could not be installed on the old box because a devel-repo
    # glibc pin blocked it; freeipmi was the workaround. Both, here.
    ipmitool
    freeipmi
    # basics not already supplied by modules/nixos/profiles/base.nix
    tmux
    htop
    python3
    ingressAcceptance
    sops
    age
  ];
}
