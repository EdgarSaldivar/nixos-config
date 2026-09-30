let
  nixpkgs = /nix/store/63im7h5dmpwj0rjghrs7mi39vssm8hp8-source;
  pkgs = import nixpkgs { system = "aarch64-linux"; };
  lib = pkgs.lib;
  module = ../nix/nixos-module.nix;
  package = pkgs.writeShellScriptBin "terracompute-ops" "exit 0";

  evaluateWith = serviceConfig: extraModules:
    import "${nixpkgs}/nixos/lib/eval-config.nix" {
      system = "aarch64-linux";
      modules = [ module ] ++ extraModules ++ [
        ({ ... }: {
          boot.isContainer = true;
          system.stateVersion = "26.05";
          services.terracomputeOps = {
            inherit package;
          } // serviceConfig;
        })
      ];
    };
  evaluate = serviceConfig: evaluateWith serviceConfig [ ];

  failedAssertionCount = evaluation:
    builtins.length (lib.filter (item: !item.assertion) evaluation.config.assertions);

  disabled = evaluate { };
  disabledExample = evaluateWith { } [ ../examples/nixos-disabled-example.nix ];
  enabled = evaluate {
    enable = true;
    collector = {
      enable = true;
      configFile = "/etc/terracompute-ops/collector.json";
      credentials = {
        ssh-identity = "/run/operator/collector-ssh";
        known-hosts = "/run/operator/known-hosts";
        vast-read-api-key = "/run/operator/vast-read";
        bmc-password = "/run/operator/bmc-read-password";
      };
    };
    notifier = {
      enable = true;
      configFile = "/etc/terracompute-ops/notifier.json";
      credentials = {
        telegram-token = "/run/operator/telegram-token";
        telegram-chat-id = "/run/operator/telegram-chat-id";
      };
    };
    operatorInput = {
      enable = true;
      configFile = "/etc/terracompute-ops/operator-input.json";
      credentials = {
        telegram-token = "/run/operator/telegram-token";
        telegram-chat-id = "/run/operator/telegram-chat-id";
      };
    };
    webhook = {
      enable = true;
      configFile = "/etc/terracompute-ops/webhook.json";
      credentials.vast-webhook-secret = "/run/operator/vast-webhook-secret";
    };
  };
  invalidCredential = evaluate {
    enable = true;
    collector = {
      enable = true;
      configFile = "/etc/terracompute-ops/collector.json";
      credentials.vast-write-api-key = "/run/operator/forbidden";
    };
  };
  invalidOperatorInputCredential = evaluate {
    enable = true;
    operatorInput = {
      enable = true;
      configFile = "/etc/terracompute-ops/operator-input.json";
      credentials.telegram-approval-key = "/run/operator/forbidden";
    };
  };
  missingOperatorInputCredential = evaluate {
    enable = true;
    operatorInput = {
      enable = true;
      configFile = "/etc/terracompute-ops/operator-input.json";
    };
  };
  fakeRestic = pkgs.writeShellScriptBin "restic" "exit 0";
  fakeOpenSSH = pkgs.writeShellScriptBin "ssh" "exit 0";
  fakeCodex = pkgs.writeShellScriptBin "codex" "exit 0";
  uncommissionedScaffolds = evaluate {
    enable = true;
    backup = {
      enable = true;
      configFile = "/etc/terracompute-ops/backup.json";
      preflightAttestationFile = "/run/terracompute-backup-preflight/published/pelargir-preflight.json";
      credentials.restic-password = "/run/operator/restic-password";
    };
    watchdog = {
      enable = true;
      configFile = "/etc/terracompute-ops/watchdog.json";
      credentials.healthchecks-ping-url = "/run/operator/healthchecks-ping-url";
    };
    investigator = {
      enable = true;
      configFile = "/etc/terracompute-ops/investigator.json";
      codexPackage = fakeCodex;
      runtimeVersion = "0.154.0";
      approvedRuntimeVersion = "0.154.0";
      runtimeClosureHash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
      approvedRuntimeClosureHash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
    };
  };
  commissionedInvestigator = {
    enable = true;
    configFile = "/etc/terracompute-ops/investigator.json";
    commissioningAttestation = "investigator-v2-linux-arm64-isolation-auth-seeding-and-named-producer-verified";
    codexPackage = fakeCodex;
    runtimeVersion = "0.154.0";
    approvedRuntimeVersion = "0.154.0";
    runtimeClosureHash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
    approvedRuntimeClosureHash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
  };
  commissioned = evaluate {
    enable = true;
    backup = {
      enable = true;
      configFile = "/etc/terracompute-ops/backup.json";
      preflightAttestationFile = "/run/terracompute-backup-preflight/published/pelargir-preflight.json";
      commissioningAttestation = "backup-v2-pelargir-receiver-and-quota-probe-verified";
      resticPackage = fakeRestic;
      opensshPackage = fakeOpenSSH;
      credentials = {
        restic-password = "/run/operator/restic-password";
        ssh-identity = "/run/operator/backup-ssh";
        known-hosts = "/run/operator/backup-known-hosts";
      };
    };
    watchdog = {
      enable = true;
      configFile = "/etc/terracompute-ops/watchdog.json";
      commissioningAttestation = "watchdog-v2-local-heartbeat-and-healthchecks-verified";
      credentials.healthchecks-ping-url = "/run/operator/healthchecks-ping-url";
    };
    investigator = commissionedInvestigator;
  };
  actionsCollector = {
    enable = true;
    configFile = "/etc/terracompute-ops/collector.json";
    credentials = {
      ssh-identity = "/run/operator/collector-ssh";
      known-hosts = "/run/operator/known-hosts";
      vast-read-api-key = "/run/operator/vast-read";
      bmc-password = "/run/operator/bmc-read-password";
    };
  };
  actionsRole = {
    enable = true;
    configFile = "/etc/terracompute-ops/actions.json";
    commissioningAttestation = "actions-v1-monitor-restart-actor-telegram-and-live-dry-check-verified";
    opensshPackage = fakeOpenSSH;
    credentials = {
      telegram-token = "/run/operator/telegram-token";
      actor-ssh-identity = "/run/operator/actor-ssh";
      actor-known-hosts = "/run/operator/actor-known-hosts";
    };
  };
  actionsCommissioned = evaluate {
    enable = true;
    collector = actionsCollector;
    backup = commissioned.config.services.terracomputeOps.backup;
    actions = actionsRole;
  };
  actionsWithoutBackup = evaluate {
    enable = true;
    collector = actionsCollector;
    actions = actionsRole;
  };
  actionsUncommissioned = evaluate {
    enable = true;
    collector = actionsCollector;
    backup = commissioned.config.services.terracomputeOps.backup;
    actions = actionsRole // { commissioningAttestation = null; };
  };
  actionsWithOperatorInput = evaluate {
    enable = true;
    collector = actionsCollector;
    backup = commissioned.config.services.terracomputeOps.backup;
    actions = actionsRole;
    operatorInput = {
      enable = true;
      configFile = "/etc/terracompute-ops/operator-input.json";
      credentials = {
        telegram-token = "/run/operator/telegram-token";
        telegram-chat-id = "/run/operator/telegram-chat-id";
      };
    };
  };
  actionsExtraCredential = evaluate {
    enable = true;
    collector = actionsCollector;
    backup = commissioned.config.services.terracomputeOps.backup;
    actions = actionsRole // { credentials = actionsRole.credentials // { vast-write-api-key = "/run/operator/forbidden"; }; };
  };
  missingPreflight = evaluate {
    enable = true;
    backup = {
      enable = true;
      configFile = "/etc/terracompute-ops/backup.json";
      commissioningAttestation = "backup-v2-pelargir-receiver-and-quota-probe-verified";
      credentials = {
        restic-password = "/run/operator/restic-password";
        ssh-identity = "/run/operator/backup-ssh";
        known-hosts = "/run/operator/backup-known-hosts";
      };
    };
  };
  wrongPreflightPath = evaluate {
    enable = true;
    backup = {
      enable = true;
      configFile = "/etc/terracompute-ops/backup.json";
      preflightAttestationFile = "/run/terracompute/pelargir-preflight.json";
      commissioningAttestation = "backup-v2-pelargir-receiver-and-quota-probe-verified";
      credentials = {
        restic-password = "/run/operator/restic-password";
        ssh-identity = "/run/operator/backup-ssh";
        known-hosts = "/run/operator/backup-known-hosts";
      };
    };
  };
  missingWatchdogChat = evaluate {
    enable = true;
    watchdog = {
      enable = true;
      configFile = "/etc/terracompute-ops/watchdog.json";
      commissioningAttestation = "watchdog-v2-local-heartbeat-and-healthchecks-verified";
    };
  };
  mismatchedInvestigator = evaluate {
    enable = true;
    investigator = commissionedInvestigator // {
      approvedRuntimeVersion = "0.155.0";
    };
  };
  # The closure half of the gate must be able to fail on its own: same version, a
  # runtime closure that is not the one approved.
  mismatchedClosure = evaluate {
    enable = true;
    investigator = commissionedInvestigator // {
      runtimeClosureHash = "sha256-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=";
    };
  };
  # The capability broker stays inert until the investigator is commissioned,
  # the broker carries its own exact commissioning string, and an attested
  # sandbox runner is configured with the exact isolation-contract string.
  fakeSandboxRunner = pkgs.writeShellScriptBin "sandbox-runner" "exit 0";
  brokerUncommissioned = evaluate {
    enable = true;
    investigator = commissionedInvestigator // {
      capabilityBroker.enable = true;
    };
  };
  # Enabling the foundation must not pretend a sandbox runner exists: the
  # commissioning string alone is not enough to turn the broker on.
  brokerWithoutSandboxRunner = evaluate {
    enable = true;
    investigator = commissionedInvestigator // {
      capabilityBroker = {
        enable = true;
        commissioningAttestation = "capability-broker-v2-fail-closed-run-request-ledger-and-typed-effects-verified";
      };
    };
  };
  # A stale attestation for the retired v1 synchronous-run contract must fail
  # closed exactly like any other wrong string: the v2 contract requires an
  # external process whose kill terminates all work, which v1 never verified.
  brokerWrongRunnerAttestation = evaluate {
    enable = true;
    investigator = commissionedInvestigator // {
      capabilityBroker = {
        enable = true;
        commissioningAttestation = "capability-broker-v2-fail-closed-run-request-ledger-and-typed-effects-verified";
        sandboxRunnerPackage = fakeSandboxRunner;
        sandboxRunnerAttestation = "sandbox-runner-v1-no-network-task-root-only-no-credential-paths-kill-on-deadline-verified";
      };
    };
  };
  brokerCommissioned = evaluate {
    enable = true;
    investigator = commissionedInvestigator // {
      capabilityBroker = {
        enable = true;
        commissioningAttestation = "capability-broker-v2-fail-closed-run-request-ledger-and-typed-effects-verified";
        sandboxRunnerPackage = fakeSandboxRunner;
        sandboxRunnerAttestation = "sandbox-runner-v2-external-process-no-network-task-root-only-no-credential-paths-kill-terminates-all-work-verified";
      };
    };
  };

  displayRole = {
    enable = true;
    configFile = "/etc/terracompute-ops/display.json";
    commissioningAttestation = "display-v1-read-only-snapshot-and-forced-command-receiver-verified";
    opensshPackage = fakeOpenSSH;
    credentials = {
      display-ssh-identity = "/run/operator/display-ssh";
      known-hosts = "/run/operator/known-hosts";
      vast-read-api-key = "/run/operator/vast-read";
    };
  };
  displayCommissioned = evaluate {
    enable = true;
    collector = actionsCollector;
    backup = commissioned.config.services.terracomputeOps.backup;
    actions = actionsRole;
    display = displayRole;
  };
  displayWithoutActions = evaluate { enable = true; collector = actionsCollector; display = displayRole; };
  displayUncommissioned = evaluate {
    enable = true;
    collector = actionsCollector;
    display = displayRole // { commissioningAttestation = null; };
  };
  # The display must never be handed the actor key, however it is named.
  displayWithActorKey = evaluate {
    enable = true;
    collector = actionsCollector;
    display = displayRole // { credentials = displayRole.credentials // { actor-ssh-identity = "/run/operator/actor-ssh"; }; };
  };
  displayWithoutCollector = evaluate { enable = true; display = displayRole; };
  displayServices = displayCommissioned.config.systemd.services;
  display = displayServices.terracompute-display.serviceConfig;
  displayAgent = displayServices.terracompute-display-agent.serviceConfig;

  services = enabled.config.systemd.services;
  collector = services.terracompute-collector.serviceConfig;
  notifier = services.terracompute-notifier.serviceConfig;
  operatorInput = services.terracompute-operator-input.serviceConfig;
  webhook = services.terracompute-webhook.serviceConfig;
  collectorExample = builtins.fromJSON (builtins.readFile ../examples/collector.json);
  notifierExample = builtins.fromJSON (builtins.readFile ../examples/notifier.json);
  operatorInputExample = builtins.fromJSON (builtins.readFile ../examples/operator-input.json);
  webhookExample = builtins.fromJSON (builtins.readFile ../examples/webhook.json);
  bmcUsernameExample = builtins.readFile ../examples/bmc-username;
  bmcPinExample = builtins.readFile ../examples/bmc-cert-sha256;
  tmpfiles = enabled.config.systemd.tmpfiles.rules;
  commissionedServices = commissioned.config.systemd.services;
  backup = commissionedServices.terracompute-backup.serviceConfig;
  preflightFetchUnit = commissionedServices.terracompute-backup-preflight-fetch;
  preflightFetch = preflightFetchUnit.serviceConfig;
  preflightPublishUnit = commissionedServices.terracompute-backup-preflight-publish;
  preflightPublish = preflightPublishUnit.serviceConfig;
  watchdog = commissionedServices.terracompute-watchdog.serviceConfig;
  investigator = commissionedServices.terracompute-investigator.serviceConfig;
  optionalTmpfiles = commissioned.config.systemd.tmpfiles.rules;
  sorted = builtins.sort builtins.lessThan;
in
assert disabled.config.services.terracomputeOps.enable == false;
assert !disabled.config.services.terracomputeOps.backup.enable;
assert !disabled.config.services.terracomputeOps.watchdog.enable;
assert !disabled.config.services.terracomputeOps.investigator.enable;
assert disabled.config.services.terracomputeOps.backup.commissioningAttestation == null;
assert disabled.config.services.terracomputeOps.watchdog.commissioningAttestation == null;
assert disabled.config.services.terracomputeOps.investigator.commissioningAttestation == null;
assert !(disabled.config.systemd.services ? terracompute-collector);
assert !(disabled.config.systemd.services ? terracompute-notifier);
assert !(disabled.config.systemd.services ? terracompute-operator-input);
assert !(disabled.config.systemd.services ? terracompute-webhook);
assert !(disabled.config.systemd.services ? terracompute-backup);
assert !(disabled.config.systemd.services ? terracompute-backup-preflight-fetch);
assert !(disabled.config.systemd.services ? terracompute-backup-preflight-publish);
assert !(disabled.config.systemd.services ? terracompute-watchdog);
assert !(disabled.config.systemd.services ? terracompute-investigator);
assert !(disabled.config.systemd.services ? terracompute-actions);
assert !disabled.config.services.terracomputeOps.actions.enable;
assert failedAssertionCount actionsCommissioned == failedAssertionCount disabled;
assert actionsCommissioned.config.systemd.services.terracompute-actions.serviceConfig.User == "terracompute-actions";
assert actionsCommissioned.config.systemd.services.terracompute-actions.serviceConfig.NoNewPrivileges;
assert actionsCommissioned.config.systemd.services.terracompute-actions.serviceConfig.Group == "terracompute-actions";
assert actionsCommissioned.config.systemd.services.terracompute-actions.serviceConfig.SupplementaryGroups == [ "terracompute-state" ];
assert sorted actionsCommissioned.config.systemd.services.terracompute-actions.serviceConfig.ReadWritePaths == sorted [
  "/var/lib/imladris/terracompute-ops"
  "/var/lib/terracompute-actions"
];
assert builtins.elem "d /var/lib/terracompute-actions 0700 terracompute-actions terracompute-actions - -"
  actionsCommissioned.config.systemd.tmpfiles.rules;
assert actionsCommissioned.config.users.users.terracompute-actions.group == "terracompute-actions";
assert failedAssertionCount actionsWithOperatorInput == failedAssertionCount disabled + 1;
assert sorted actionsCommissioned.config.systemd.services.terracompute-actions.serviceConfig.LoadCredential == sorted [
  "actor-known-hosts:/run/operator/actor-known-hosts"
  "actor-ssh-identity:/run/operator/actor-ssh"
  "telegram-token:/run/operator/telegram-token"
];
assert lib.hasInfix
  (builtins.unsafeDiscardStringContext "--ssh-executable ${fakeOpenSSH}/bin/ssh")
  (builtins.unsafeDiscardStringContext actionsCommissioned.config.systemd.services.terracompute-actions.serviceConfig.ExecStart);
assert failedAssertionCount actionsWithoutBackup == failedAssertionCount disabled + 1;
assert failedAssertionCount actionsUncommissioned == failedAssertionCount disabled + 1;
assert !(actionsUncommissioned.config.systemd.services ? terracompute-actions);
assert failedAssertionCount actionsExtraCredential == failedAssertionCount disabled + 1;
assert !(actionsExtraCredential.config.systemd.services ? terracompute-actions);
assert !(disabled.config.systemd.timers ? terracompute-backup);
assert !(disabled.config.systemd.timers ? terracompute-watchdog);
assert !(disabled.config.systemd.paths ? terracompute-backup-expedited);
assert !disabledExample.config.services.terracomputeOps.enable;
assert !(disabledExample.config.systemd.services ? terracompute-collector);
assert !(disabledExample.config.systemd.services ? terracompute-operator-input);
assert disabledExample.config.environment.etc."terracompute-ops/collector.json".user == "root";
assert disabledExample.config.environment.etc."terracompute-ops/collector.json".group == "root";
assert disabledExample.config.environment.etc."terracompute-ops/collector.json".mode == "0444";
assert failedAssertionCount disabled == 0;
assert failedAssertionCount enabled == failedAssertionCount disabled;
assert failedAssertionCount invalidCredential == failedAssertionCount enabled + 1;
assert failedAssertionCount invalidOperatorInputCredential == failedAssertionCount enabled + 1;
assert failedAssertionCount missingOperatorInputCredential == failedAssertionCount enabled + 1;
assert failedAssertionCount uncommissionedScaffolds == failedAssertionCount disabled + 3;
assert !(uncommissionedScaffolds.config.systemd.services ? terracompute-backup);
assert !(uncommissionedScaffolds.config.systemd.services ? terracompute-watchdog);
assert !(uncommissionedScaffolds.config.systemd.services ? terracompute-investigator);
assert failedAssertionCount commissioned == failedAssertionCount disabled;
assert commissioned.config.services.terracomputeOps.observationOnly;
assert commissioned.config.services.terracomputeOps.targetMachineId == "17049";
assert !commissioned.config.services.terracomputeOps.investigator.collectorIngress;
assert commissioned.config.services.terracomputeOps.investigator.credentials == { };
assert commissioned.config.systemd.services ? terracompute-backup;
assert commissioned.config.systemd.services ? terracompute-backup-preflight-fetch;
assert commissioned.config.systemd.services ? terracompute-backup-preflight-publish;
assert commissioned.config.systemd.services ? terracompute-watchdog;
assert commissioned.config.systemd.services ? terracompute-investigator;
assert failedAssertionCount missingPreflight == failedAssertionCount disabled + 1;
assert failedAssertionCount wrongPreflightPath == failedAssertionCount disabled + 1;
assert failedAssertionCount missingWatchdogChat == failedAssertionCount disabled + 1;
assert failedAssertionCount mismatchedInvestigator == failedAssertionCount disabled + 1;
assert failedAssertionCount mismatchedClosure == failedAssertionCount disabled + 1;
assert !(missingPreflight.config.systemd.services ? terracompute-backup);
assert !(wrongPreflightPath.config.systemd.services ? terracompute-backup);
assert !(missingWatchdogChat.config.systemd.services ? terracompute-watchdog);
assert !(mismatchedInvestigator.config.systemd.services ? terracompute-investigator);
assert !(mismatchedClosure.config.systemd.services ? terracompute-investigator);
assert backup.User == "terracompute-backup";
assert backup.Group == "terracompute-backup";
assert preflightFetch.User == "terracompute-preflight";
assert preflightFetch.Group == "terracompute-preflight";
assert preflightPublish.User == "root";
assert preflightPublish.Group == "terracompute-backup";
assert preflightPublish.SupplementaryGroups == [ "terracompute-preflight" ];
assert watchdog.User == "terracompute-watchdog";
assert watchdog.Group == "terracompute-watchdog";
assert builtins.elem "terracompute-state" commissioned.config.users.users.terracompute-watchdog.extraGroups;
assert investigator.User == "terracompute-investigator";
assert investigator.Group == "terracompute-investigator";
assert sorted backup.LoadCredential == sorted [
  "known-hosts:/run/operator/backup-known-hosts"
  "restic-password:/run/operator/restic-password"
  "ssh-identity:/run/operator/backup-ssh"
];
assert sorted preflightFetch.LoadCredential == sorted [
  "known-hosts:/run/operator/backup-known-hosts"
  "ssh-identity:/run/operator/backup-ssh"
];
assert preflightPublish.LoadCredential == [ ];
assert sorted preflightPublish.InaccessiblePaths == sorted [
  "/run/operator/backup-known-hosts"
  "/run/operator/backup-ssh"
  "/run/operator/restic-password"
];
assert watchdog.LoadCredential == [
  "healthchecks-ping-url:/run/operator/healthchecks-ping-url"
];
assert investigator.LoadCredential == [ ];
assert investigator.Restart == "always";
assert builtins.match ".*/bin/terracompute-backup --config /etc/terracompute-ops/backup.json --preflight-attestation /run/terracompute-backup-preflight/published/pelargir-preflight.json --restic-executable .*/bin/restic --restic-password-file %d/restic-password --ssh-executable .*/bin/ssh --ssh-identity-file %d/ssh-identity --ssh-known-hosts-file %d/known-hosts" backup.ExecStart != null;
assert builtins.match ".*/bin/terracompute-backup-preflight fetch --config /etc/terracompute-ops/backup.json --incoming /run/terracompute-backup-preflight/incoming/pelargir-preflight.json --sftp-executable .*/bin/sftp --ssh-identity-file %d/ssh-identity --ssh-known-hosts-file %d/known-hosts" preflightFetch.ExecStart != null;
assert builtins.match ".*/bin/terracompute-backup-preflight publish --config /etc/terracompute-ops/backup.json --incoming /run/terracompute-backup-preflight/incoming/pelargir-preflight.json --publication /run/terracompute-backup-preflight/published/pelargir-preflight.json" preflightPublish.ExecStart != null;
assert builtins.match ".*/bin/terracompute-watchdog --config /etc/terracompute-ops/watchdog.json --healthchecks-ping-url-file %d/healthchecks-ping-url" watchdog.ExecStart != null;
assert builtins.match ".*/bin/terracompute-investigator --config /etc/terracompute-ops/investigator.json --codex-executable .*/bin/codex" investigator.ExecStart != null;
assert backup.ProtectSystem == "strict";
assert watchdog.ProtectSystem == "strict";
assert investigator.ProtectSystem == "strict";
assert backup.NoNewPrivileges && watchdog.NoNewPrivileges && investigator.NoNewPrivileges;
assert preflightFetch.NoNewPrivileges && preflightPublish.NoNewPrivileges;
assert backup.MemoryMax > 0 && watchdog.MemoryMax > 0 && investigator.MemoryMax > 0;
assert backup.TasksMax <= 256 && watchdog.TasksMax <= 256 && investigator.TasksMax <= 256;
assert builtins.elem "/var/lib/imladris/terracompute-ops" backup.ReadOnlyPaths;
assert builtins.elem "/var/lib/imladris/terracompute-ops/backups" backup.ReadWritePaths;
assert preflightFetch.ReadOnlyPaths == [ "/" ];
assert preflightFetch.ReadWritePaths == [ "/run/terracompute-backup-preflight/incoming" ];
assert preflightPublish.ReadOnlyPaths == [
  "/"
  "/run/terracompute-backup-preflight/incoming"
];
assert preflightPublish.ReadWritePaths == [ "/run/terracompute-backup-preflight/published" ];
assert preflightPublish.RestrictAddressFamilies == [ "AF_UNIX" ];
assert preflightPublish.IPAddressDeny == "any";
assert preflightPublish.PrivateNetwork;
assert watchdog.ReadWritePaths == [ "/var/lib/terracompute-watchdog" ];
assert watchdog.ReadOnlyPaths == [ "/var/lib/imladris/terracompute-ops" ];
assert investigator.ReadWritePaths == [
  "/var/lib/imladris/terracompute-codex"
  "/var/lib/terracompute-investigator"
];
assert commissioned.config.systemd.services.terracompute-investigator.environment.HOME == "/var/lib/imladris/terracompute-codex";
assert !(commissioned.config.systemd.services.terracompute-investigator.environment ? CODEX_HOME);
# The capability broker is off by default and leaves the deployed unit untouched.
assert !disabled.config.services.terracomputeOps.investigator.capabilityBroker.enable;
assert disabled.config.services.terracomputeOps.investigator.capabilityBroker.commissioningAttestation == null;
assert disabled.config.services.terracomputeOps.investigator.capabilityBroker.sandboxRunnerPackage == null;
assert disabled.config.services.terracomputeOps.investigator.capabilityBroker.sandboxRunnerAttestation == null;
assert !commissioned.config.services.terracomputeOps.investigator.capabilityBroker.enable;
assert !(commissioned.config.systemd.services.terracompute-investigator.environment ? TERRACOMPUTE_CAPABILITY_BROKER);
assert failedAssertionCount brokerUncommissioned == failedAssertionCount disabled + 1;
# The commissioning string alone must not enable the broker: without an
# attested sandbox runner (or with the wrong runner attestation) it fails.
assert failedAssertionCount brokerWithoutSandboxRunner == failedAssertionCount disabled + 1;
assert failedAssertionCount brokerWrongRunnerAttestation == failedAssertionCount disabled + 1;
assert failedAssertionCount brokerCommissioned == failedAssertionCount disabled;
assert lib.hasSuffix "/bin/sandbox-runner"
  brokerCommissioned.config.systemd.services.terracompute-investigator.environment.TERRACOMPUTE_BROKER_SANDBOX_RUNNER;
assert brokerCommissioned.config.systemd.services.terracompute-investigator.environment.TERRACOMPUTE_CAPABILITY_BROKER == "1";
assert brokerCommissioned.config.systemd.services.terracompute-investigator.environment.TERRACOMPUTE_BROKER_WORKSPACE_ROOT
  == "/var/lib/imladris/terracompute-codex/task-workspaces";
assert brokerCommissioned.config.systemd.services.terracompute-investigator.environment.TERRACOMPUTE_BROKER_STATE_ROOT
  == "/var/lib/terracompute-investigator/broker";
assert brokerCommissioned.config.systemd.services.terracompute-investigator.environment.HOME == "/var/lib/imladris/terracompute-codex";
assert !(commissioned.config.systemd.services.terracompute-investigator.environment ? OPENAI_API_KEY);
assert !(commissioned.config.systemd.services ? terracompute-evidence-tools);
assert commissioned.config.users.users.terracompute-investigator.home == "/var/lib/imladris/terracompute-codex";
assert commissioned.config.systemd.timers.terracompute-backup.timerConfig.OnCalendar == "hourly";
assert commissioned.config.systemd.timers.terracompute-backup.timerConfig.Persistent;
assert commissioned.config.systemd.paths.terracompute-backup-expedited.pathConfig.Unit == "terracompute-backup.service";
assert commissioned.config.systemd.paths.terracompute-backup-expedited.pathConfig.PathChanged == "/var/lib/imladris/terracompute-ops/backup-expedited.trigger";
assert builtins.elem "terracompute-backup-preflight-fetch.service" commissionedServices.terracompute-backup.requires;
assert builtins.elem "terracompute-backup-preflight-publish.service" commissionedServices.terracompute-backup.requires;
assert builtins.elem "terracompute-backup-preflight-fetch.service" commissionedServices.terracompute-backup.after;
assert builtins.elem "terracompute-backup-preflight-publish.service" commissionedServices.terracompute-backup.after;
assert preflightPublishUnit.requires == [ ];
assert preflightPublishUnit.wants == [ "terracompute-backup-preflight-fetch.service" ];
assert preflightPublishUnit.after == [ "terracompute-backup-preflight-fetch.service" ];
assert preflightFetch.UMask == "0027";
assert preflightPublish.SupplementaryGroups == [ "terracompute-preflight" ];
assert commissioned.config.systemd.timers.terracompute-watchdog.timerConfig.OnUnitActiveSec == "30s";
assert builtins.elem "d /var/lib/terracompute-watchdog 0700 terracompute-watchdog terracompute-watchdog - -" optionalTmpfiles;
assert builtins.elem "d /run/terracompute-backup-preflight/incoming 0750 terracompute-preflight terracompute-preflight - -" optionalTmpfiles;
assert builtins.elem "d /run/terracompute-backup-preflight/published 0750 root terracompute-backup - -" optionalTmpfiles;
assert builtins.elem "d /var/lib/terracompute-investigator/requests/pending 0700 terracompute-investigator terracompute-investigator - -" optionalTmpfiles;
assert builtins.elem "d /var/lib/terracompute-investigator/results/completed 0700 terracompute-investigator terracompute-investigator - -" optionalTmpfiles;
assert builtins.elem "f /var/lib/terracompute-investigator/database/investigator.sqlite3 0600 terracompute-investigator terracompute-investigator - -" optionalTmpfiles;
assert enabled.config.services.terracomputeOps.stateRoot == "/var/lib/imladris/terracompute-ops";
assert !(enabled.config.systemd.timers ? terracompute-ops);
assert collector.User == "terracompute-collector";
assert notifier.User == "terracompute-notifier";
assert operatorInput.User == "terracompute-operator-input";
assert webhook.User == "terracompute-webhook";
assert collector.User != notifier.User;
assert notifier.User != operatorInput.User;
assert operatorInput.User != webhook.User;
assert collector.Group == "terracompute-state";
assert notifier.Group == "terracompute-state";
assert operatorInput.Group == "terracompute-state";
assert webhook.Group == "terracompute-state";
assert collector.UMask == "0007";
assert builtins.elem "/var/lib/imladris/terracompute-ops" collector.ReadWritePaths;
assert builtins.match ".*terracompute-ops daemon --config.*" collector.ExecStart != null;
assert builtins.match ".*terracompute-ops notify --config.*" notifier.ExecStart != null;
assert builtins.match ".*terracompute-ops operator-input --config.*" operatorInput.ExecStart != null;
assert builtins.match ".*terracompute-ops webhook --config.*" webhook.ExecStart != null;
assert sorted collector.LoadCredential == sorted [
  "bmc-password:/run/operator/bmc-read-password"
  "known-hosts:/run/operator/known-hosts"
  "ssh-identity:/run/operator/collector-ssh"
  "vast-read-api-key:/run/operator/vast-read"
];
assert sorted notifier.LoadCredential == sorted [
  "telegram-chat-id:/run/operator/telegram-chat-id"
  "telegram-token:/run/operator/telegram-token"
];
assert sorted operatorInput.LoadCredential == sorted [
  "telegram-chat-id:/run/operator/telegram-chat-id"
  "telegram-token:/run/operator/telegram-token"
];
assert webhook.LoadCredential == [ "vast-webhook-secret:/run/operator/vast-webhook-secret" ];
assert !(builtins.elem "terracompute-operator-input.service" enabled.config.systemd.services.terracompute-notifier.after);
assert !(builtins.elem "terracompute-operator-input.service" enabled.config.systemd.services.terracompute-notifier.requires);
assert webhook.IPAddressDeny == "any";
assert webhook.IPAddressAllow == [ "localhost" ];
assert collector.TasksMax > notifier.TasksMax;
assert collector.MemoryMax > notifier.MemoryMax;
assert !(collector ? RestrictNamespaces);
assert builtins.stringLength enabled.config.systemd.units."terracompute-collector.service".text > 0;
assert builtins.stringLength enabled.config.systemd.units."terracompute-notifier.service".text > 0;
assert builtins.stringLength enabled.config.systemd.units."terracompute-operator-input.service".text > 0;
assert builtins.stringLength enabled.config.systemd.units."terracompute-webhook.service".text > 0;
assert builtins.elem "d /var/lib/imladris/terracompute-ops 2771 root terracompute-state - -" tmpfiles;
assert builtins.elem "d /var/lib/imladris/terracompute-ops/incidents 2770 root terracompute-state - -" tmpfiles;
assert collectorExample.machine_id == "17049";
assert collectorExample.sources.ssh.target == "terracompute-observer@10.50.0.2";
assert collectorExample.sources.prometheus.endpoint == "http://10.50.0.2:9090";
assert bmcUsernameExample == "palantir\n";
assert bmcPinExample == "9097629B14F1AD21A5959AE9BB77524E05D8EA3C3FD0F32A92A139EEB9EE1514\n";
assert notifierExample.telegram.group_id == -1004484415005;
assert !notifierExample.telegram.input_enabled;
assert operatorInputExample.telegram.group_id == notifierExample.telegram.group_id;
assert operatorInputExample.telegram.input_enabled;
assert operatorInputExample.telegram.inbox_path == "/var/lib/imladris/terracompute-ops/operator-input/inbox.sqlite3";
assert webhookExample.webhook.host == "127.0.0.1";
assert !(disabled.config.systemd.services ? terracompute-display);
assert !(disabled.config.systemd.timers ? terracompute-display);
assert failedAssertionCount displayCommissioned == failedAssertionCount disabled;
assert failedAssertionCount displayUncommissioned == failedAssertionCount disabled + 1;
assert failedAssertionCount displayWithActorKey == failedAssertionCount disabled + 1;
assert failedAssertionCount displayWithoutCollector == failedAssertionCount disabled + 1;
assert !(displayUncommissioned.config.systemd.services ? terracompute-display);
assert display.User == "terracompute-display";
assert display.Group == "terracompute-display";
assert display.SupplementaryGroups == [ "terracompute-state" ];
assert builtins.elem "terracompute-state" displayCommissioned.config.users.users.terracompute-display.extraGroups;
assert sorted display.LoadCredential == sorted [
  "display-ssh-identity:/run/operator/display-ssh"
  "known-hosts:/run/operator/known-hosts"
  "vast-read-api-key:/run/operator/vast-read"
];
assert display.ReadOnlyPaths == [ "/var/lib/imladris/terracompute-ops" "/run/terracompute-display" ];
assert display.ReadWritePaths == [ "/var/lib/terracompute-display" ];
assert display.Type == "oneshot";
assert displayCommissioned.config.systemd.timers.terracompute-display.timerConfig.OnUnitActiveSec == "30s";
assert displayAgent.User == "terracompute-actions";
assert displayAgent.PrivateNetwork;
assert displayAgent.IPAddressDeny == "any";
assert !(displayAgent ? LoadCredential);
assert displayAgent.InaccessiblePaths == [ "-/run/credentials" ];
assert displayAgent.ReadOnlyPaths == [ "/var/lib/terracompute-actions" ];
assert displayAgent.ReadWritePaths == [ "/run/terracompute-display" ];
assert builtins.elem "d /run/terracompute-display 2750 terracompute-actions terracompute-display - -"
  displayCommissioned.config.systemd.tmpfiles.rules;
assert !(displayWithoutActions.config.systemd.services ? terracompute-display-agent);
assert displayWithoutActions.config.systemd.services.terracompute-display.serviceConfig.ReadOnlyPaths
  == [ "/var/lib/imladris/terracompute-ops" ];
assert builtins.stringLength displayCommissioned.config.systemd.units."terracompute-display.service".text > 0;
assert builtins.stringLength displayCommissioned.config.systemd.units."terracompute-display-agent.service".text > 0;

true
