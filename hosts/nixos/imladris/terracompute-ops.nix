{ config, pkgs, inputs, ... }:
let
  # Standalone source commit f80b2b82c03c601630aaf488858efcf1e0eab1df.
  source = ../../../vendor/terracompute-ops;
  package = pkgs.callPackage "${source}/default.nix" { };
  json = name: value: pkgs.writeText "terracompute-${name}.json" (builtins.toJSON value);
  stateDir = "/var/lib/imladris/terracompute-ops";
  investigatorRoot = "/var/lib/terracompute-investigator";
  # The one package taken from nixpkgs-codex. The app-server protocol and the
  # model list move far faster than a NixOS release, and 26.05 pins 0.133.0,
  # which does not offer the models the investigator asks for.
  codex = inputs.nixpkgs-codex.legacyPackages.${pkgs.stdenv.hostPlatform.system}.codex;
  # Measured from the closure built on this machine:
  #   nix path-info -r <codex> | LC_ALL=C sort | sha256sum, as SRI base64
  codexClosure = "sha256-uph7AJo7dEz1JRhWfTxtuAUSH5vWozUbumuOMK9/Ssg=";
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
    # Diagnosis by a model rather than by the one rule taught by hand. The runtime
    # is observation-only: it reads a question from its spool, asks the app server,
    # and writes an answer back. It has no remediation interface of any kind.
    investigator = {
      enable = true;
      configFile = json "investigator" {
        schema_version = 1;
        observation_only = true;
        machine_id = "17049";
        commissioning_attestation =
          "investigator-v2-linux-arm64-isolation-auth-seeding-and-named-producer-verified";
        request_spool = "${investigatorRoot}/requests";
        result_spool = "${investigatorRoot}/results";
        database_path = "${investigatorRoot}/database/investigator.sqlite3";
        service_home = "/var/lib/imladris/terracompute-codex";
        poll_seconds = 1;
        turn_timeout_seconds = 600;
        max_spool_entries = 128;
        # The one other user whose questions it answers. Its uid is allocated when
        # the machine activates, so the runtime resolves this name at startup.
        producer_user = "terracompute-actions";
      };
      commissioningAttestation =
        "investigator-v2-linux-arm64-isolation-auth-seeding-and-named-producer-verified";
      codexPackage = codex;
      # Measured and approved together, so a version or closure that drifts from
      # what was reviewed leaves the service uncommissioned rather than running:
      #   nix build --no-link --print-out-paths <codex>
      #   nix path-info -r <path> | LC_ALL=C sort | sha256sum, as SRI base64
      runtimeVersion = codex.version;
      approvedRuntimeVersion = "0.154.0";
      runtimeClosureHash = codexClosure;
      approvedRuntimeClosureHash = codexClosure;
      # The action service may ask it what is wrong. It becomes the runtime's one
      # named producer and reaches nothing else; see docs/INVESTIGATOR-RUNTIME.md.
      actionsIngress = true;
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
      # Paused 2026-09-17 while the approval loop was reworked: proposals now wait
      # for an answer instead of expiring, and diagnosis moves to the investigator.
      # Resumed 2026-09-17 with that rework reviewed and its waiting made durable;
      # it asks the investigator and still asks a person before any restart.
      enable = true;
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
        # Ask for every restart until the loop has proven itself here. Turning this on
        # lets it restart dcgm-exporter by itself, within its own daily allowance.
        self_service = false;
        # Ask the investigator what is wrong, falling back to the rule when it is
        # unavailable or does not answer in time. Asking never blocks the loop.
        investigator = true;
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
