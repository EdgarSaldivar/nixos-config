# pelargir — quiesced k3s PVC backup to minas over Tailscale/SFTP.
{ config, pkgs, ... }:
let
  kubectl = "${pkgs.k3s}/bin/k3s kubectl";
  deployments = "home-assistant zigbee2mqtt mosquitto";
  scaleDown = pkgs.writeShellScript "pelargir-scale-down" ''
    set -eu
    for deployment in ${deployments}; do
      ${kubectl} -n home scale "deployment/$deployment" --replicas=0
      ${kubectl} -n home rollout status "deployment/$deployment" --timeout=180s
    done
  '';
  scaleUp = pkgs.writeShellScript "pelargir-scale-up" ''
    # ExecStopPost/postStop reaches here even when restic fails. Try every
    # deployment independently so one API error cannot strand the others down.
    status=0
    for deployment in ${deployments}; do
      ${kubectl} -n home scale "deployment/$deployment" --replicas=1 || status=1
    done
    exit "$status"
  '';
  # ---------------------------------------------------------------------------
  #  k3s datastore — the ONLY copy of cluster state
  # ---------------------------------------------------------------------------
  # Until 2026-08-06 this was backed up by NOTHING. `paths` covers the restic
  # staging dir and the prepare command copied /var/lib/rancher/k3s/storage
  # (the PVCs) — but never /var/lib/rancher/k3s/server/db. Losing pelargir's
  # NVMe therefore lost the cluster outright: every object, every Secret, and
  # the identity every agent joined against. It is 22 MB.
  #
  # ⚠️  DO NOT "simplify" this into an rsync of the db directory. The datastore
  # runs in WAL mode, and when measured it was a 22 MB state.db alongside a
  # 30 MB state.db-wal — i.e. MORE committed data sat in the WAL than in the
  # main file. A file copy races all three files (db/-wal/-shm) independently
  # and yields a torn database that may restore silently wrong.
  #
  # `.backup` uses SQLite's online backup API, is safe against the live writer,
  # and measured at under a second. k3s is NOT stopped — verified working while
  # the API server was serving.
  #
  # The server token is copied too, and that is not optional: restoring the
  # datastore without the matching token gives a cluster no existing agent can
  # rejoin, because the token is what their credentials were derived from.
  # Both live inside the 0700 staging dir and restic encrypts the repository.
  k3sDatastore = pkgs.writeShellScript "pelargir-k3s-datastore-backup" ''
    set -eu
    staging=/var/lib/restic-staging/pelargir/k3s-datastore
    install -d -m 0700 "$staging"
    db=/var/lib/rancher/k3s/server/db/state.db

    if [ ! -f "$db" ]; then
      echo "CRITICAL: k3s datastore $db not found" >&2
      exit 1
    fi

    # Write to .tmp and VALIDATE before replacing the last known-good copy.
    # An empty-but-valid SQLite file passes integrity_check, so row count is
    # checked too — the same failure mode the sqlite dumps on minas guard against.
    ${pkgs.sqlite}/bin/sqlite3 -cmd ".timeout 30000" "$db" \
      ".backup '$staging/state.db.tmp'"

    ok=$(${pkgs.sqlite}/bin/sqlite3 "$staging/state.db.tmp" \
           "PRAGMA integrity_check;" 2>/dev/null || echo bad)
    rows=$(${pkgs.sqlite}/bin/sqlite3 "$staging/state.db.tmp" \
             "SELECT COUNT(*) FROM kine;" 2>/dev/null || echo 0)

    if [ "$ok" != "ok" ] || [ "''${rows:-0}" -lt 1 ]; then
      rm -f "$staging/state.db.tmp"
      echo "CRITICAL: k3s datastore copy failed validation (integrity=$ok rows=$rows)" >&2
      exit 1
    fi

    mv "$staging/state.db.tmp" "$staging/state.db"
    echo "k3s datastore backed up ($rows kine rows)"

    # Restoring needs this as much as the database itself.
    install -m 0600 /var/lib/rancher/k3s/server/token "$staging/token"

    # Record what this copy came from; a restore into a different k3s version
    # is not guaranteed to work, and "which version was this?" is exactly the
    # question you cannot answer at 1am from the file alone.
    {
      echo "taken:    $(date -u -Is)"
      echo "k3s:      $(${pkgs.k3s}/bin/k3s --version 2>/dev/null | head -1)"
      echo "kine_rows: $rows"
    } > "$staging/PROVENANCE.txt"
  '';

  preflight = pkgs.writeShellScript "pelargir-restic-preflight" ''
    set -eu
    if ! ${pkgs.tailscale}/bin/tailscale status --json \
      | ${pkgs.jq}/bin/jq -e \
          'any(.Peer[]?; .HostName == "minas-tirith" and .Online == true)' \
          >/dev/null; then
      echo "SKIP: minas-tirith is not online in the tailnet"
      exit 1
    fi
    if ! ${pkgs.openssh}/bin/sftp -b /dev/null \
      -i /etc/ssh/ssh_host_ed25519_key \
      -o BatchMode=yes -o ConnectTimeout=10 \
      pelargir-backup@minas-tirith; then
      echo "SKIP: minas SFTP account is unreachable"
      exit 1
    fi
  '';
in
{
  services.restic.backups.minas = {
    repository = "sftp:pelargir-backup@minas-tirith:/backups/pelargir";
    passwordFile = config.sops.secrets.restic_password.path;
    # Reuse the stable host identity already required for sops bootstrap; minas
    # authorizes only its public half with an internal-sftp restriction.
    extraOptions = [
      "sftp.command='ssh -i /etc/ssh/ssh_host_ed25519_key pelargir-backup@minas-tirith -s sftp'"
    ];
    initialize = false;
    paths = [ "/var/lib/restic-staging/pelargir" ];
    timerConfig = {
      OnCalendar = "*-*-* 03:30:00";
      RandomizedDelaySec = "30m";
      Persistent = true;
    };
    pruneOpts = [
      "--keep-daily 7"
      "--keep-weekly 5"
      "--keep-monthly 12"
    ];
    runCheck = false;

    backupPrepareCommand = ''
      set -eu
      # Datastore FIRST, deliberately: it needs no quiescing (online .backup),
      # so doing it before scaleDown means a datastore failure fails the run
      # without having taken Home Assistant, Z2M and Mosquitto down first.
      ${k3sDatastore}
      ${scaleDown}
      staging=/var/lib/restic-staging/pelargir
      install -d -m 0700 "$staging/pvcs" "$staging/home-assistant-config"
      ${pkgs.rsync}/bin/rsync -a --delete \
        /var/lib/rancher/k3s/storage/ "$staging/pvcs/"
      for source in /var/lib/rancher/k3s/storage/*_home_home-assistant-config; do
        [ -d "$source" ] || continue
        ${pkgs.rsync}/bin/rsync -a --delete "$source/" "$staging/home-assistant-config/"
      done
    '';
    # The restic module maps this to postStop (ExecStopPost), so it runs after
    # success, backup failure, or a failed prepare command.
    backupCleanupCommand = ''
      ${scaleUp}
    '';
  };

  systemd.services.restic-backups-minas = {
    # ExecCondition's 1 means "skipped", not failed. This is the clean
    # skip-not-fail behavior required when the remote site is asleep. The same
    # probe is also the explicit ExecStartPre gate requested for the backup;
    # the near-immediate second check closes the race before quiescing PVCs.
    serviceConfig = {
      ExecCondition = preflight;
      ExecStartPre = preflight;
    };
  };

  systemd.services.restic-check-minas = {
    description = "Weekly integrity check of pelargir's restic repository";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      ExecCondition = preflight;
      # The generated wrapper carries repository, password file, and the
      # restricted SFTP command, keeping this weekly check identical to backup.
      ExecStart = "/run/current-system/sw/bin/restic-minas check";
    };
  };
  systemd.timers.restic-check-minas = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "Sun *-*-* 05:00:00";
      RandomizedDelaySec = "30m";
      Persistent = true;
    };
  };
}
