# minas-tirith — restricted SFTP destination for pelargir's restic backups.
#
# This closes docs/runbooks/pelargir/backup.md §2/§3. Until 2026-08-06 the destination simply did
# not exist, and the failure mode was the dangerous kind: pelargir's
# `restic-backups-minas.timer` and `restic-check-minas.timer` were both ENABLED,
# but their preflight treats an unreachable SFTP account as a clean SKIP (exit 1
# from ExecCondition means "skipped", not "failed"). So pelargir reported healthy
# while nothing was ever backed up off-host. A backup that fails loudly is an
# incident; one that skips quietly is a surprise years later.
#
# ⚠️  docs/runbooks/pelargir/backup.md records that this was once `useradd` + a hand-made authorized_keys
# file. That was written while minas ran openSUSE. It is now NixOS, so the same
# thing is expressed declaratively here instead — an imperative `useradd` would
# be reverted or shadowed by the next activation.
{ config, ... }:
{
  # ---------------------------------------------------------------------------
  #  The account
  # ---------------------------------------------------------------------------
  # Deliberately NOT a member of any group that can read service data. It exists
  # only to own one directory. `isSystemUser` keeps it out of the normal login
  # range, and a null shell means there is nothing to get even if the sshd
  # restrictions below were somehow bypassed.
  users.groups.pelargir-backup = { };
  users.users.pelargir-backup = {
    isSystemUser = true;
    group = "pelargir-backup";
    home = "/backups/pelargir";
    createHome = false; # created by tmpfiles below, with modes we control
    shell = "/run/current-system/sw/bin/nologin";
    description = "restic backup target for pelargir (SFTP only)";

    # Authorized with pelargir's SSH **HOST** key, not a user key.
    #
    # This looks unusual and is deliberate — it is what pelargir's restic actually
    # presents (`sftp.command='ssh -i /etc/ssh/ssh_host_ed25519_key ...'` in
    # hosts/nixos/pelargir/backup.nix). Reusing that already-required, already-
    # managed identity avoids inventing a second secret that would need its own
    # generation, distribution, sops entry and rotation story. The key is public;
    # only pelargir holds the private half.
    #
    # Fingerprint (verify before changing):
    #   SHA256:RiqSHXqCwOJN6udIy7JgsWgSbeSJXm00QeVngbXg2Yc
    openssh.authorizedKeys.keys = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK2f3tPyGAX8T9MHnxTAsxizIH3JO9AXad1Ld7GxabWI pelargir-host-2026-08-03"
    ];
  };

  # ---------------------------------------------------------------------------
  #  The directory
  # ---------------------------------------------------------------------------
  # /backups is root-owned and NOT writable by the account; only the repository
  # one level down is. That layout is what a future ChrootDirectory would need,
  # so adopting it now means enabling chroot later is a one-line change rather
  # than a migration of the repository path.
  #
  # ⚠️  /backups MUST be the dataset `storage2/backups`, not a directory on the
  # root NVMe. This receives another host's entire backup history; the NVMe is a
  # single non-redundant device already >50% full and at 33% wear, while storage2
  # is redundant and checksummed.
  #
  # Created once by hand (disko is forbidden from touching zpools on this host,
  # see ./disko.nix), exactly like storage2/backup in INSTALL-RUNBOOK §8:
  #     sudo zfs create -o mountpoint=/backups storage2/backups
  #
  # Nothing here can create it, and that is the point of the guard below.
  systemd.tmpfiles.rules = [
    "d /backups         0755 root             root             -"
    "d /backups/pelargir 0700 pelargir-backup pelargir-backup -"
  ];

  # If the dataset is not mounted, tmpfiles cheerfully creates /backups on the
  # ROOT filesystem and restic writes there — silently, successfully, and onto
  # the one device this whole arrangement exists to avoid depending on. The
  # symptom would be a slowly filling NVMe and a backup with no redundancy,
  # discovered at the worst possible time.
  #
  # Same failure this host already guards against for storage2/backup in
  # ./backup-root-data.nix, which aborts rather than snapshotting an unmounted path.
  systemd.services.backups-mount-guard = {
    description = "Verify /backups is the ZFS dataset, not the root disk";
    wantedBy = [ "multi-user.target" ];
    after = [ "zfs-import-storage2.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      src=$(${config.boot.zfs.package}/bin/zfs list -H -o name,mounted storage2/backups 2>/dev/null || echo "")
      case "$src" in
        *"	yes") echo "/backups is the storage2/backups dataset — OK" ;;
        *)
          echo "CRITICAL: /backups is NOT the storage2/backups dataset." >&2
          echo "  restic would write pelargir's backups onto the root NVMe." >&2
          echo "  Fix: sudo zfs create -o mountpoint=/backups storage2/backups" >&2
          exit 1
          ;;
      esac
    '';
  };

  # ---------------------------------------------------------------------------
  #  sshd restriction
  # ---------------------------------------------------------------------------
  # ForceCommand internal-sftp means this account cannot obtain a shell even
  # with a valid key — sshd runs the in-process SFTP server and nothing else.
  # The remaining options remove every other thing an SSH session can be used
  # for, so a compromised pelargir host key yields file access to one directory
  # and no pivot into this host or its network.
  #
  # NOTE: this host is reachable from the internet via a NAT forward
  # (minas.saldivar.io:2222 -> :22), so these restrictions are load-bearing, not
  # defence in depth for a LAN-only service.
  services.openssh.extraConfig = ''
    Match User pelargir-backup
      ForceCommand internal-sftp
      PasswordAuthentication no
      KbdInteractiveAuthentication no
      AllowTcpForwarding no
      AllowAgentForwarding no
      AllowStreamLocalForwarding no
      PermitTTY no
      PermitTunnel no
      X11Forwarding no
    Match all
  '';
}
