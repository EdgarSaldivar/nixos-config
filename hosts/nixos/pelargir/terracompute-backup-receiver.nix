# pelargir — disabled-by-default restricted receiver for Terracompute state.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.terracomputeBackupReceiver;
  repository = "/backups/terracompute-ops";
  attestation = "/backups/terracompute-preflight.json";
  quota = 250 * 1024 * 1024 * 1024;
  reserve = 100 * 1024 * 1024 * 1024;
  makeAttestation = pkgs.writeShellScript "terracompute-backup-attestation" ''
    set -eu
    repository=${lib.escapeShellArg repository}
    quota=${toString quota}
    reserve=${toString reserve}

    used=$(${pkgs.coreutils}/bin/du -sb -- "$repository" | ${pkgs.coreutils}/bin/cut -f1)
    filesystem_free=$(
      ${pkgs.coreutils}/bin/df -B1 --output=avail -- "$repository" \
        | ${pkgs.coreutils}/bin/tail -n 1 \
        | ${pkgs.coreutils}/bin/tr -d ' '
    )
    case "$used:$filesystem_free" in
      *[!0-9:]*|:*) exit 1 ;;
    esac

    logical_free=$((quota > used ? quota - used : 0))
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
  options.services.terracomputeBackupReceiver = {
    enable = lib.mkEnableOption "the restricted Terracompute SFTP backup receiver";
    authorizedKey = lib.mkOption {
      type = lib.types.nullOr lib.types.nonEmptyStr;
      default = null;
      description = "Dedicated public SSH key used only by the Imladris backup sender.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion =
          cfg.authorizedKey != null
          && builtins.match "ssh-ed25519 [A-Za-z0-9+/]+={0,3}( [^\\n]*)?" cfg.authorizedKey != null;
        message = "terracomputeBackupReceiver requires one dedicated ssh-ed25519 public key";
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

    systemd.tmpfiles.rules = [
      "d /backups 0711 root root -"
      "d ${repository} 0700 terracompute-backup terracompute-backup -"
    ];

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
      after = [ "local-fs.target" ];
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
