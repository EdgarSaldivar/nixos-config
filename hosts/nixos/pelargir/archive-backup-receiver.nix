# pelargir — disabled-by-default restricted SFTP receiver for one external
# sender's encrypted backups.
#
# ⛔ The runtime names below (user, group, paths, units, script names) are
# deliberately left as they were first deployed. Renaming any of them changes
# pelargir's unit files, restarts the volume unit and remounts the repository;
# do it only as a planned migration, never as cleanup.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.archiveBackupReceiver;
  repository = "/backups/terracompute-ops";
  attestation = "/backups/terracompute-preflight.json";
  quota = 250 * 1024 * 1024 * 1024;
  reserve = 100 * 1024 * 1024 * 1024;

  # The quota is a filesystem, not a number in a report: the repository is its own
  # ext4 image of exactly `quota` bytes, loop-mounted over ${repository}. A faulty
  # or compromised sender can grow the repository to `quota` and no further, so
  # it can never take pelargir's root filesystem (the only k3s control plane)
  # beyond that. The image is sparse and grows into that root filesystem as it
  # fills -- which is why `reserve`
  # is still measured on the host filesystem below. That reserve is advisory (a
  # cooperating sender honours the attestation); the hard guarantee is the cap.
  volumeDir = "/var/lib/terracompute-backup";
  volumeImage = "${volumeDir}/volume.img";
  mountOptions = "loop,nosuid,nodev,noexec";
  mountVolume = pkgs.writeShellScript "terracompute-backup-volume" ''
    set -eu
    PATH=${
      lib.makeBinPath [
        pkgs.coreutils
        pkgs.diffutils
        pkgs.e2fsprogs
        pkgs.findutils
        pkgs.gnugrep
        pkgs.procps
        pkgs.util-linux
      ]
    }
    repository=${lib.escapeShellArg repository}
    image=${lib.escapeShellArg volumeImage}
    original="$repository.pre-volume"

    settle() { # the mounted volume root is the sender's, whatever happened before
      chown terracompute-backup:terracompute-backup "$repository"
      chmod 0700 "$repository"
    }
    if mountpoint -q "$repository"; then
      # Only accept the volume itself here, never some other mount.
      src=$(findmnt -n -o SOURCE --target "$repository")
      if ! losetup -j "$image" -n -O NAME | grep -qxF "$src"; then
        echo "terracompute-backup-volume: $repository is mounted from $src, not $image" >&2
        exit 1
      fi
      settle
      exit 0
    fi
    install -d -m 0700 -o root -g root ${lib.escapeShellArg volumeDir}
    install -d -m 0700 -o root -g root "$repository"

    # Lock the sender out for the rest of this script. New sessions can no longer
    # resolve the directory, and live ones are ended so no open handle survives
    # into the copy (or keeps writing to the root filesystem afterwards).
    chown root:root "$repository"
    chmod 0700 "$repository"
    pkill -KILL -u terracompute-backup || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      pgrep -u terracompute-backup >/dev/null || break
      sleep 1
    done
    if pgrep -u terracompute-backup >/dev/null; then
      echo "terracompute-backup-volume: sender processes did not exit" >&2
      exit 1
    fi

    if [ ! -e "$image" ]; then
      if [ -e "$original" ]; then
        echo "terracompute-backup-volume: $original exists but $image does not;" >&2
        echo "  an earlier migration was interrupted -- finish it by hand" >&2
        exit 1
      fi
      # Leftovers of an attempt that died mid-way: never delete an image that is
      # still attached to a loop device (its blocks would stay allocated).
      for dev in $(losetup -j "$image.new" -n -O NAME 2>/dev/null); do
        findmnt -rn -S "$dev" -o TARGET | xargs -r -n1 umount
        losetup -d "$dev"
      done
      rm -f "$image.new"

      # The copy lands on the same root filesystem the image grows into: refuse
      # unless it fits with ext4 overhead (+10%) and still leaves the reserve.
      need=$(( $(du -sb -- "$repository" | cut -f1) * 11 / 10 + ${toString reserve} ))
      free=$(df -B1 --output=avail -- ${lib.escapeShellArg volumeDir} | tail -n 1 | tr -d ' ')
      if [ "$free" -lt "$need" ]; then
        echo "terracompute-backup-volume: migration needs $need bytes free, have $free" >&2
        exit 1
      fi

      staging=$(mktemp -d /run/terracompute-backup-volume.XXXXXX)
      cleanup() {
        if mountpoint -q "$staging" && ! umount "$staging"; then
          # Still attached: keep the image so the loop-device recovery above can
          # find it by name on the next start.
          echo "terracompute-backup-volume: could not unmount $staging" >&2
          return
        fi
        rmdir "$staging" 2>/dev/null || true
        rm -f "$image.new"
      }
      trap cleanup EXIT
      truncate -s ${toString quota} "$image.new"
      mkfs.ext4 -q -m 0 -L tc-backup "$image.new"
      mount -o ${mountOptions} "$image.new" "$staging"
      # restic owns the repository root; an ext4 lost+found there is noise.
      rmdir "$staging/lost+found"
      # One-time copy of a repository that predates the volume.
      if [ -n "$(ls -A "$repository")" ]; then
        cp -a "$repository/." "$staging/"
        diff -r "$repository" "$staging" >/dev/null
      fi
      # Owned correctly before it is published, so no later interruption can leave
      # a mounted but root-only repository behind.
      chown terracompute-backup:terracompute-backup "$staging"
      chmod 0700 "$staging"
      umount "$staging"
      sync
      # Published: from here on the volume is the repository.
      mv -T "$image.new" "$image"
      trap - EXIT
      rmdir "$staging"
    fi

    # The pre-volume repository is kept beside the mountpoint, untouched, until an
    # operator has verified a restore from the volume and deletes it.
    if [ -n "$(ls -A "$repository")" ]; then
      if [ -e "$original" ]; then
        echo "terracompute-backup-volume: both $repository and $original hold data" >&2
        exit 1
      fi
      mv -T "$repository" "$original"
      install -d -m 0700 -o root -g root "$repository"
    fi

    # Unmounted, the mountpoint stays root-owned 0700, so a push that races a
    # failed mount is refused instead of landing on the root filesystem.
    mount -o ${mountOptions} "$image" "$repository"
    settle
  '';

  makeAttestation = pkgs.writeShellScript "terracompute-backup-attestation" ''
    set -eu
    repository=${lib.escapeShellArg repository}
    quota=${toString quota}
    reserve=${toString reserve}

    ${pkgs.util-linux}/bin/mountpoint -q -- "$repository" || exit 1
    used=$(${pkgs.coreutils}/bin/du -sb -- "$repository" | ${pkgs.coreutils}/bin/cut -f1)
    avail() {
      ${pkgs.coreutils}/bin/df -B1 --output=avail -- "$1" \
        | ${pkgs.coreutils}/bin/tail -n 1 \
        | ${pkgs.coreutils}/bin/tr -d ' '
    }
    # The volume's own free space, and the host filesystem the sparse image grows into.
    volume_free=$(avail "$repository")
    filesystem_free=$(avail ${lib.escapeShellArg volumeDir})
    case "$used:$volume_free:$filesystem_free" in
      *[!0-9:]*|:*|*::*|*:) exit 1 ;;
    esac

    logical_free=$((quota > used ? quota - used : 0))
    logical_free=$((volume_free < logical_free ? volume_free : logical_free))
    host_free=$((filesystem_free > reserve ? filesystem_free - reserve : 0))
    if [ "$host_free" -lt "$logical_free" ]; then
      free="$host_free"
    else
      free="$logical_free"
    fi

    measured=$(${pkgs.coreutils}/bin/date -u +%Y-%m-%dT%H:%M:%SZ)
    expires=$(${pkgs.coreutils}/bin/date -u -d '+10 minutes' +%Y-%m-%dT%H:%M:%SZ)
    temporary=$(${pkgs.coreutils}/bin/mktemp /backups/.terracompute-preflight.XXXXXX)
    trap '${pkgs.coreutils}/bin/rm -f -- "$temporary"' EXIT
    ${pkgs.jq}/bin/jq -cn \
      --arg measured "$measured" \
      --arg expires "$expires" \
      --argjson quota "$quota" \
      --argjson free "$free" \
      '{
        schema_version: 1,
        kind: "pelargir-sftp-quota-preflight-v1",
        machine_id: "17049",
        commissioning_attestation: "backup-v2-pelargir-receiver-and-quota-probe-verified",
        repository: "sftp:terracompute-backup@pelargir:/terracompute-ops",
        quota_bytes: $quota,
        free_bytes: $free,
        measured_at: $measured,
        expires_at: $expires
      }' >"$temporary"
    ${pkgs.coreutils}/bin/chmod 0444 "$temporary"
    ${pkgs.coreutils}/bin/chown root:root "$temporary"
    ${pkgs.coreutils}/bin/mv -fT -- "$temporary" ${lib.escapeShellArg attestation}
    trap - EXIT
  '';
in
{
  options.services.archiveBackupReceiver = {
    enable = lib.mkEnableOption "the restricted SFTP backup receiver";
    authorizedKey = lib.mkOption {
      type = lib.types.nullOr lib.types.nonEmptyStr;
      default = null;
      description = "Dedicated public SSH key used only by the backup sender.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion =
          cfg.authorizedKey != null
          && builtins.match "ssh-ed25519 [A-Za-z0-9+/]+={0,3}( [^\\n]*)?" cfg.authorizedKey != null;
        message = "archiveBackupReceiver requires one dedicated ssh-ed25519 public key";
      }
    ];

    users.groups.terracompute-backup = { };
    users.users.terracompute-backup = {
      isSystemUser = true;
      group = "terracompute-backup";
      home = repository;
      createHome = false;
      shell = "${pkgs.shadow}/bin/nologin";
      openssh.authorizedKeys.keys = lib.optional (cfg.authorizedKey != null) cfg.authorizedKey;
    };

    # ${repository} itself is managed by terracompute-backup-volume: tmpfiles must
    # not chown the bare mountpoint to the sender.
    systemd.tmpfiles.rules = [
      "d /backups 0711 root root -"
    ];

    systemd.services.terracompute-backup-volume = {
      description = "Mount the size-capped Terracompute backup volume";
      wantedBy = [ "multi-user.target" ];
      after = [ "local-fs.target" ];
      before = [
        "sshd.service"
        "terracompute-backup-attestation.service"
      ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = mountVolume;
        ExecStop = "${pkgs.util-linux}/bin/umount ${repository}";
      };
    };

    services.openssh.extraConfig = ''
      Match User terracompute-backup
        ChrootDirectory /backups
        ForceCommand internal-sftp -d /terracompute-ops
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

    systemd.services.terracompute-backup-attestation = {
      description = "Publish Terracompute backup receiver quota attestation";
      after = [ "terracompute-backup-volume.service" ];
      requires = [ "terracompute-backup-volume.service" ];
      serviceConfig = {
        Type = "oneshot";
        UMask = "0022";
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectHome = true;
        ProtectSystem = "strict";
        ReadWritePaths = [ "/backups" ];
      };
      script = "${makeAttestation}";
    };
    systemd.timers.terracompute-backup-attestation = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "30s";
        OnUnitActiveSec = "1min";
        AccuracySec = "5s";
        Persistent = false;
      };
    };
  };
}
