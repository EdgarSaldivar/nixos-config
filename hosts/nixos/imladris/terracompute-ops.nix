{ config, pkgs, ... }:
let
  # Standalone source commit d9434988005784e50eab3114415d5c730ca16efe.
  source = ../../../vendor/terracompute-ops;
  package = pkgs.callPackage "${source}/default.nix" { };
  json = name: value: pkgs.writeText "terracompute-${name}.json" (builtins.toJSON value);
  stateDir = "/var/lib/imladris/terracompute-ops";
  credentials = config.sops.secrets;
in
{
  imports = [ "${source}/nix/nixos-module.nix" ];
  environment.etc = {
    "terracompute-ops/bmc-username".text = "palantir\n";
    "terracompute-ops/bmc-cert-sha256".text =
      "90:97:62:9B:14:F1:AD:21:A5:95:9A:E9:BB:77:52:4E:05:D8:EA:3C:3F:D0:F3:2A:92:A1:39:EE:B9:EE:15:14\n";
  };
  services.terracomputeOps = {
    # Explicit commissioning latch. Everything below remains inert until the
    # VPN, target SSH, Prometheus, notification and restore gates pass.
    enable = false;
    inherit package;
    collector = {
      enable = true;
      configFile = json "collector" {
        state_dir = stateDir;
        machine_id = "17049";
        sources = {
          ssh = {
            enabled = true;
            target = "terracompute-observer@10.50.0.2";
            identity_file = "/run/credentials/terracompute-collector.service/ssh-identity";
            known_hosts_file = "/run/credentials/terracompute-collector.service/known-hosts";
          };
          prometheus = {
            enabled = true;
            endpoint = "http://10.50.0.2:9090";
            vast_exporter_job = "prometheus";
            dcgm_exporter_job = "Terracompute";
            max_age_seconds = 180;
          };
          vast = {
            enabled = true;
            api_key_file = "/run/credentials/terracompute-collector.service/vast-read-api-key";
          };
          bmc = {
            enabled = true;
            username_file = "/etc/terracompute-ops/bmc-username";
            password_file = "/run/credentials/terracompute-collector.service/bmc-password";
            cert_sha256_file = "/etc/terracompute-ops/bmc-cert-sha256";
          };
        };
        webhook = {
          enabled = false;
          queue_path = "${stateDir}/webhook.sqlite3";
        };
      };
      credentials = {
        ssh-identity = credentials.terracompute-ssh-identity.path;
        known-hosts = credentials.terracompute-known-hosts.path;
        vast-read-api-key = credentials.terracompute-vast-read-api-key.path;
        bmc-password = credentials.terracompute-bmc-password.path;
      };
    };
    notifier = {
      enable = true;
      configFile = json "notifier" {
        state_dir = stateDir;
        machine_id = "17049";
        telegram = {
          enabled = true;
          token_file = "/run/credentials/terracompute-notifier.service/telegram-token";
          chat_id_file = "/run/credentials/terracompute-notifier.service/telegram-chat-id";
          group_id = -1004484415005;
          input_enabled = false;
        };
      };
      credentials = {
        telegram-token = credentials.terracompute-telegram-bot-token.path;
        telegram-chat-id = credentials.terracompute-telegram-chat-id.path;
      };
    };
    backup = {
      enable = true;
      configFile = json "backup" {
        schema_version = 1;
        observation_only = true;
        machine_id = "17049";
        commissioning_attestation = "backup-v2-pelargir-receiver-and-quota-probe-verified";
        state_dir = stateDir;
        snapshot_root = "${stateDir}/backups";
        repository = "sftp:terracompute-backup@pelargir:/terracompute-ops";
        repository_quota_bytes = 268435456000;
        minimum_quota_free_bytes = 5368709120;
        lock_file = "${stateDir}/backups/backup.lock";
        deadline_seconds = 600;
      };
      preflightAttestationFile = "/run/terracompute-backup-preflight/published/pelargir-preflight.json";
      commissioningAttestation = "backup-v2-pelargir-receiver-and-quota-probe-verified";
      credentials = {
        restic-password = credentials.terracompute-backup-restic-password.path;
        ssh-identity = credentials.terracompute-backup-ssh-identity.path;
        known-hosts = credentials.terracompute-backup-known-hosts.path;
      };
    };
    watchdog = {
      enable = true;
      configFile = json "watchdog" {
        schema_version = 1;
        observation_only = true;
        machine_id = "17049";
        commissioning_attestation = "watchdog-v2-local-heartbeat-and-healthchecks-verified";
        handoff_file = "${stateDir}/controller-heartbeat.json";
        state_file = "/var/lib/terracompute-watchdog/state/evaluator.json";
        operation_seconds = 30;
      };
      commissioningAttestation = "watchdog-v2-local-heartbeat-and-healthchecks-verified";
      credentials.healthchecks-ping-url = credentials.terracompute-healthchecks-ping-url.path;
    };
  };
}
