{ config, pkgs, ... }:
let
  # Standalone source commit d0a4f760c467d0404f1a39eda23aa21b7eb95ca9.
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
            binary = "${pkgs.openssh}/bin/ssh";
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
      # Keep delivery disabled while commissioning. The outbox remains durable,
      # and enabling this is the explicit final notification cutover.
      enable = false;
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
        # While Telegram delivery is disabled, notification progress is expected to
        # be stale; requiring it would keep Healthchecks failing and hide a real
        # collection stall. Enabling the notifier restores the requirement.
        notification_progress_required = config.services.terracomputeOps.notifier.enable;
      };
      commissioningAttestation = "watchdog-v2-local-heartbeat-and-healthchecks-verified";
      credentials.healthchecks-ping-url = "/run/secrets/terracompute-healthchecks-ping-url";
    };
    actions = {
      # Approval-gated dcgm-exporter restart for a blocked GPU VM handover. Enable only
      # after the actor account, helper and restricted key are verified on the target
      # (terracompute-ops docs/MONITOR-RESTART-ACTION.md, commissioning steps 3 and 4).
      # It consumes Telegram updates itself, so operator input must stay disabled.
      # Commissioned 2026-09-17: actor helper f9ac29e6 and key
      # SHA256:IkDRGKaU7jnh9GBwQe1UCzjKkpyBXeoXlcCzh+DMXC0 installed and verified
      # (status only; other commands, PTY and forwarding refused).
      #
      # Paused 2026-09-17 while the approval loop is reworked: proposals must wait
      # for an answer instead of expiring, and diagnosis moves to the investigator.
      # The target actor stays installed; only the controller service is off.
      enable = false;
      configFile = json "actions" {
        schema_version = 1;
        machine_id = "17049";
        commissioning_attestation = "actions-v1-monitor-restart-actor-telegram-and-live-dry-check-verified";
        state_database = "${stateDir}/state.sqlite3";
        actions_database = "/var/lib/terracompute-actions/actions.sqlite3";
        inbox_path = "/var/lib/terracompute-actions/telegram-inbox.sqlite3";
        backup_trigger_file = "${stateDir}/backup-expedited.trigger";
        actor_target = "terracompute-actor@10.50.0.2";
        telegram_group_id = -1004484415005;
        telegram_bot_username = "TerraComputeBot";
        policy_revision = "monitor-restart-r1";
        tick_seconds = 15;
      };
      commissioningAttestation = "actions-v1-monitor-restart-actor-telegram-and-live-dry-check-verified";
      credentials = {
        telegram-token = "/run/secrets/terracompute-telegram-bot-token";
        actor-ssh-identity = "/run/secrets/terracompute-actor-ssh-identity";
        # The actor reaches the same target sshd as the observer, so the host key pin
        # is the observer's.
        actor-known-hosts = "/run/secrets/terracompute-known-hosts";
      };
    };
  };
}
