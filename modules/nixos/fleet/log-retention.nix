# Fleet log retention — a TIME cap on the logs a host keeps, not just a size cap.
#
# Owner decision 2026-10-23: PinCollector's privacy page says server logs are
# kept for 30 days. Before this module both log stores on the k3s hosts were
# capped by size alone, and size is not time:
#
#   journald         SystemMaxUse=500M on pelargir, the 10%-of-disk default on
#                    minas. Measured 2026-10-23: pelargir held 27 days, minas held
#                    78 (back to 2026-08-06).
#   /var/log/pods    kubelet rotates by size only (10Mi x 5 files) and has no age
#                    setting at all. traefik fills that in about a day, but a quiet
#                    container such as PinCollector's postgres keeps one 0.log for
#                    as long as its Pod lives.
#
# Both are capped here. Neither touches k3s itself: kubelet-arg changes restart
# the k3s agent and with it every Pod on the host (see minas-tirith/k3s.nix), and
# kubelet has no age flag to set anyway.
#
# ⚠️ Enabling this DELETES what is already past the cap, on the first journald
# restart and the first timer run. That is the point, and it is not reversible.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.fleet.logRetention;
  day = 86400;
in
{
  options.fleet.logRetention = {
    enable = lib.mkEnableOption "a time cap on journald and Kubernetes container logs";

    maxAgeDays = lib.mkOption {
      type = lib.types.ints.between 2 3650;
      default = 30;
      description = ''
        The age no log line on this host may exceed. This is the promise, not a
        target: each store below is configured so that it deletes a line before
        the line reaches this age, never after.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    # Measured against systemd 260's source (journal-vacuum.c, journald.c): vacuum
    # deletes an ARCHIVED file once its OLDEST entry is older than MaxRetentionSec,
    # and journald wakes itself at exactly that moment rather than waiting for the
    # next write. The ACTIVE file is never vacuumed, so it has to be rotated out
    # first: MaxFileSec=1day does that well inside the cap (the default is 1month,
    # which on its own would let a line live ~30.4 days). Lines therefore go when
    # they are between maxAgeDays-1 and maxAgeDays old. The existing size caps stay.
    services.journald.extraConfig = ''
      MaxFileSec=1day
      MaxRetentionSec=${toString cfg.maxAgeDays}day
    '';

    # Only where kubelet writes container logs. Pruned to one day under the cap,
    # hourly, so a line goes when it is 29-30 days old and the cap still holds
    # through a missed day of runs (Persistent catches up after downtime).
    systemd.services.pod-log-retention = lib.mkIf config.services.k3s.enable {
      description = "Delete Kubernetes container log lines older than ${toString cfg.maxAgeDays} days";
      unitConfig.ConditionPathIsDirectory = "/var/log/pods";
      environment = {
        POD_LOG_ROOT = "/var/log/pods";
        POD_LOG_MAX_AGE_SECONDS = toString ((cfg.maxAgeDays - 1) * day);
      };
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${pkgs.python3}/bin/python3 ${./scripts/pod-log-retention.py}";
        TimeoutStartSec = "15m";
        Nice = 19;
        IOSchedulingClass = "idle";
        # It rewrites files under /var/log/pods and nothing else.
        ProtectSystem = "strict";
        ReadWritePaths = [ "/var/log/pods" ];
        ProtectHome = true;
        PrivateTmp = true;
        PrivateNetwork = true;
        NoNewPrivileges = true;
      };
    };

    systemd.timers.pod-log-retention = lib.mkIf config.services.k3s.enable {
      description = "Hourly age cap on Kubernetes container logs";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = "hourly";
        Persistent = true;
        RandomizedDelaySec = "5m";
      };
    };
  };
}
