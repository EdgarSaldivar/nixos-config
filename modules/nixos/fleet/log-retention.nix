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
# Both are capped here, and ONLY these two: the cap covers journald and
# /var/log/pods. Left on size rotation, none of them holding PinCollector data:
# nginx's logrotate (the Beszel gate on minas), wtmp/btmp, and k3s's own
# agent/containerd/containerd.log (container lifecycle events, no requests).
#
# Neither cap touches k3s itself: kubelet-arg changes restart the k3s agent and
# with it every Pod on the host (see minas-tirith/k3s.nix), and kubelet has no
# age flag to set anyway.
#
# ⚠️ Enabling this DELETES what is already past the cap, on the first journald
# restart and the first timer run. That is the point, and it is not reversible.
# It also takes some YOUNGER journald lines once: vacuum deletes a whole archived
# file when its FIRST entry passes the cap, so a file that spans the boundary
# goes with its newer lines. On minas at enable time that is a system file
# reaching to about 2026-09-27 and a user-1000 file reaching to about 2026-10-08.
# After that, daily rotation keeps every file within one day.
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
    # next write. An ACTIVE file is never vacuumed, so it has to be archived first.
    # MaxFileSec=1day archives a file that is still being written (the default is
    # 1month, which alone would let a line live ~30.4 days) -- but it is checked
    # only on a write, so an IDLE journal is never archived by it: minas'
    # user-30001.journal sat active from its last write on 2026-09-23. The daily
    # journal-rotate below closes that hole. Lines therefore go when they are
    # between maxAgeDays-1 and maxAgeDays old. The existing size caps stay.
    services.journald.extraConfig = ''
      MaxFileSec=1day
      MaxRetentionSec=${toString cfg.maxAgeDays}day
    '';

    # `journalctl --rotate` asks journald for a full rotation (manager_full_rotate in
    # journald-manager.c): it archives the system journal, every open user journal
    # AND every user journal it does not have open, then vacuums. Daily, so no
    # file's first entry is ever more than a day older than the last rotation.
    systemd.services.journal-rotate = {
      description = "Archive every journal file so the retention cap reaches idle ones";
      after = [ "systemd-journald.service" ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${config.systemd.package}/bin/journalctl --rotate";
      };
    };

    systemd.timers.journal-rotate = {
      description = "Daily journal rotation for the retention cap";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = "daily";
        Persistent = true;
        RandomizedDelaySec = "15m";
      };
    };

    # Only where kubelet writes container logs. Pruned to one day under the cap,
    # hourly, so a line goes when it is 29-30 days old and the cap still holds
    # through a missed day of runs (Persistent catches up after downtime).
    #
    # `kubectl logs` reads only a container's live N.log. After a trim, which
    # happens only to a file whose first line is 29 days old, it shows lines
    # written since; the kept recent tail is in the rotated file beside it.
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
        # It rewrites files under /var/log/pods and nothing else. Root, because
        # kubelet's files are root-owned 0640, but with no capabilities beyond
        # owning them.
        ProtectSystem = "strict";
        ReadWritePaths = [ "/var/log/pods" ];
        ProtectHome = true;
        PrivateTmp = true;
        PrivateNetwork = true;
        NoNewPrivileges = true;
        CapabilityBoundingSet = "";
        RestrictAddressFamilies = "none";
        SystemCallFilter = [ "@system-service" ];
        SystemCallArchitectures = "native";
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        ProtectClock = true;
        LockPersonality = true;
        RestrictNamespaces = true;
        UMask = "0077";
        MemoryMax = "512M";
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
