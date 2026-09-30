{
  config,
  pkgs,
  inputs,
  ...
}:
let
  # Standalone source commit f6e10d4f283668d5222e1ec06a965c2ce77b2527.
  source = ../../../vendor/terracompute-ops;
  package = pkgs.callPackage "${source}/default.nix" { };
  json = name: value: pkgs.writeText "terracompute-${name}.json" (builtins.toJSON value);
  stateDir = "/var/lib/imladris/terracompute-ops";
  investigatorRoot = "/var/lib/terracompute-investigator";
  # The one package taken from nixpkgs-codex. The app-server protocol and the
  # model list move far faster than a NixOS release, and 26.05 pins 0.133.0,
  # which does not offer the models the investigator asks for.
  codex = inputs.nixpkgs-codex.legacyPackages.${pkgs.stdenv.hostPlatform.system}.codex;
  # The runtime's identity, derived from the package actually being built, and the
  # one that was reviewed, as a literal. Both used to be this same literal, so the
  # gate compared a value with itself and could not fail on drift.
  #
  # The store path is input-addressed: any change to codex or anything in its build
  # closure gives a new path, so hashing the path pins the closure at evaluation
  # time without building it. Re-approve after reviewing a new codex with:
  #   nix eval --raw .#nixosConfigurations.imladris.config.services.terracomputeOps.investigator.runtimeClosureHash
  codexClosure = builtins.convertHash {
    hash = builtins.hashString "sha256" (builtins.unsafeDiscardStringContext codex.outPath);
    hashAlgo = "sha256";
    toHashFormat = "sri";
  };
  # /nix/store/xgk86cjswvppfbqml8233dg6vzjxyafj-codex-0.154.0, running on imladris
  # since 2026-09-26.
  approvedCodexClosure = "sha256-OtTEChg0jd2TGm8E3quDv9UHy3mGPb+kAVKR71OrFLs=";
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
            # 17049 runs no DCGM exporter: a GPU-holding exporter blocks the
            # VFIO handover that Vast VM rentals need.
            dcgm_exporter_job = null;
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
        commissioning_attestation = "investigator-v2-linux-arm64-isolation-auth-seeding-and-named-producer-verified";
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
      commissioningAttestation = "investigator-v2-linux-arm64-isolation-auth-seeding-and-named-producer-verified";
      codexPackage = codex;
      # A version or closure that drifts from what was reviewed leaves the service
      # uncommissioned (evaluation fails) rather than running. See codexClosure.
      runtimeVersion = codex.version;
      approvedRuntimeVersion = "0.154.0";
      runtimeClosureHash = codexClosure;
      approvedRuntimeClosureHash = approvedCodexClosure;
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
    display = {
      # Terra, the console on the host's monitor (github.com/EdgarSaldivar/terracompute-terra).
      # Every 30 s a read-only snapshot goes to the host with a key it accepts only for
      # `terra receive`; nothing here can change the controller, the host or a rental.
      # See terracompute-ops docs/DISPLAY.md. Needs Terra installed on 17049 first, or
      # each push is refused.
      enable = true;
      configFile = json "display" {
        state_dir = stateDir;
        actions_db = "/var/lib/terracompute-actions/actions.sqlite3";
        agent_status = "/run/terracompute-display/agent.json";
        work_dir = "/var/lib/terracompute-display";
        display_tz = "America/Los_Angeles";
        # From the operator map (terracompute-ops docs/ONSITE-MAPPING.md). No physical
        # left-to-right order is recorded, so the screen pairs cards by PSU.
        cards = [
          { index = 0; pci_bdf = "0000:01:00.0"; psu = "C"; }
          { index = 1; pci_bdf = "0000:24:00.0"; psu = "D"; }
          { index = 2; pci_bdf = "0000:41:00.0"; psu = "D"; }
          { index = 3; pci_bdf = "0000:61:00.0"; psu = "C"; }
          { index = 4; pci_bdf = "0000:81:00.0"; psu = "A"; }
          { index = 5; pci_bdf = "0000:a1:00.0"; psu = "A"; }
          { index = 6; pci_bdf = "0000:c1:00.0"; psu = "B"; }
          { index = 7; pci_bdf = "0000:e1:00.0"; psu = "B"; }
        ];
        # A, B and D are Dell D1200E-S0. C's replacement has an unknown rating.
        psu_capacity_w = { A = 1200; B = 1200; D = 1200; };
        push = {
          target = "terracompute-display@10.50.0.2";
          identity_file = "/run/credentials/terracompute-display.service/display-ssh-identity";
          known_hosts_file = "/run/credentials/terracompute-display.service/known-hosts";
        };
        vast = {
          api_key_file = "/run/credentials/terracompute-display.service/vast-read-api-key";
          every_seconds = 300;
        };
      };
      commissioningAttestation = "display-v1-read-only-snapshot-and-forced-command-receiver-verified";
      credentials = {
        # Its public half is ./terracompute-display.pub, which Terra's installer locks to
        # `terra receive` on the host.
        display-ssh-identity = "/run/secrets/terracompute-display-ssh-identity";
        # The same target sshd as the observer, so the same host key pin.
        known-hosts = "/run/secrets/terracompute-known-hosts";
        vast-read-api-key = "/run/secrets/terracompute-vast-read-api-key";
      };
    };
  };
}
