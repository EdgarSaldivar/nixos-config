# Observation-only supervisor for Vast.ai machine 17049. This module is kept
# host-local because imladris is its sole controller; modules/README.md requires a
# second active consumer before an option-bearing capability moves into fleet/.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.terracomputeOps;
  package = pkgs.callPackage ../../../pkgs/terracompute-ops { };
in
{
  options.services.terracomputeOps = {
    # Commissioning requires four encrypted credentials plus the independently
    # verified tunnel and forced-command target helper. Keep an import of this
    # module harmless until those prerequisites exist; enabling it is the final
    # commissioning step, not something an unrelated imladris rebuild should do.
    enable = lib.mkEnableOption "the observation-only terracompute supervisor";

    observationOnly = lib.mkOption {
      type = lib.types.bool;
      default = true;
      readOnly = true;
      description = "Permanent v1 safety latch: collection and outbound notification only.";
    };

    targetMachineId = lib.mkOption {
      type = lib.types.str;
      default = "17049";
      readOnly = true;
      description = "Vast.ai machine identity that every normalized probe must report.";
    };

    sshTarget = lib.mkOption {
      type = lib.types.nonEmptyStr;
      default = "terracompute-observer@10.50.0.2";
      description = "Forced-command, read-only SSH target reached through the operator tunnel.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.observationOnly;
        message = "terracompute-ops v1 must remain observation-only";
      }
      {
        assertion = cfg.targetMachineId == "17049";
        message = "terracompute-ops may only observe Vast.ai machine 17049";
      }
      {
        assertion = builtins.match "[a-z_][a-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9.:-]*" cfg.sshTarget != null;
        message = "terracompute-ops sshTarget must be a plain user@host value";
      }
    ];

    systemd.services.terracompute-ops = {
      description = "Observe terracompute machine 17049 and drain its notification outbox";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      unitConfig.RequiresMountsFor = [ "/var/lib/imladris" ];

      serviceConfig = {
        Type = "oneshot";
        ExecStart = ''
          ${package}/bin/terracompute-ops run \
            --state-dir /var/lib/imladris/terracompute-ops \
            --machine-id ${cfg.targetMachineId} \
            --ssh-target ${lib.escapeShellArg cfg.sshTarget} \
            --ssh-binary ${pkgs.openssh}/bin/ssh \
            --ssh-identity %d/ssh-identity \
            --known-hosts %d/known-hosts \
            --telegram-token %d/telegram-token \
            --telegram-chat-id %d/telegram-chat-id
        '';
        LoadCredential = [
          "ssh-identity:${config.sops.secrets.terracompute-ssh-identity.path}"
          "known-hosts:${config.sops.secrets.terracompute-known-hosts.path}"
          "telegram-token:${config.sops.secrets.terracompute-telegram-bot-token.path}"
          "telegram-chat-id:${config.sops.secrets.terracompute-telegram-chat-id.path}"
        ];
        TimeoutStartSec = "60s";
        MemoryMax = "256M";
        TasksMax = 32;

        DynamicUser = true;
        StateDirectory = "imladris/terracompute-ops";
        StateDirectoryMode = "0700";
        UMask = "0077";
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectClock = true;
        ProtectHostname = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        ProtectProc = "invisible";
        ProcSubset = "pid";
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        CapabilityBoundingSet = "";
        SystemCallArchitectures = "native";
        RestrictAddressFamilies = [
          "AF_UNIX"
          "AF_INET"
          "AF_INET6"
        ];
      };
    };

    systemd.timers.terracompute-ops = {
      description = "Run the bounded terracompute observation supervisor";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "5min";
        OnUnitActiveSec = "5min";
        AccuracySec = "30s";
        RandomizedDelaySec = "30s";
        Persistent = false;
      };
    };
  };
}
