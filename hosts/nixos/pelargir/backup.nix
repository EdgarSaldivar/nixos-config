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
