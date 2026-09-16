{ config, pkgs, ... }:
let
  # Standalone source commit 36f87ac63dfb953b3afa160fbf24fb77f949acc4.
  source = ../../../vendor/terracompute-ops;
  package = pkgs.callPackage "${source}/default.nix" { };
  json = name: value: pkgs.writeText "terracompute-${name}.json" (builtins.toJSON value);
  stateDir = "/var/lib/imladris/terracompute-ops";
in
{
  imports = [ "${source}/nix/nixos-module.nix" ];
  # The transport is commissioned first so target identity and probe contracts
  # can be verified before any controller role starts.
  services.terracomputeL2tp.enable = true;
  environment.etc = {
    "terracompute-ops/bmc-username".text = "palantir\n";
    "terracompute-ops/bmc-cert-sha256".text =
      "9097629B14F1AD21A5959AE9BB77524E05D8EA3C3FD0F32A92A139EEB9EE1514\n";
  };
  services.terracomputeOps = {
    # Explicit commissioning latch. Everything below remains inert until the
    # VPN, target SSH, Prometheus, notification and restore gates pass.
    enable = true;
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
        ssh-identity = "/run/secrets/terracompute-ssh-identity";
        known-hosts = "/run/secrets/terracompute-known-hosts";
        vast-read-api-key = "/run/secrets/terracompute-vast-read-api-key";
        bmc-password = "/run/secrets/terracompute-bmc-password";
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
        telegram-token = "/run/secrets/terracompute-telegram-bot-token";
        telegram-chat-id = "/run/secrets/terracompute-telegram-chat-id";
      };
    };
    backup = {
      # Commission the observer, notifications, and watchdog before the first
      # repository initialization and restore exercise.
      enable = false;
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
        restic-password = "/run/secrets/terracompute-backup-restic-password";
        ssh-identity = "/run/secrets/terracompute-backup-ssh-identity";
        known-hosts = "/run/secrets/terracompute-backup-known-hosts";
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
      credentials.healthchecks-ping-url = "/run/secrets/terracompute-healthchecks-ping-url";
    };
  };
}
