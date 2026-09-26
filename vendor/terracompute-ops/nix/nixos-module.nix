# Disabled-by-default NixOS definitions for the observation-only Terracompute
# controller and its independently commissioned optional runtimes.
{ config, lib, pkgs, ... }:
let
  cfg = config.services.terracomputeOps;
  boundaries = import ./service-boundaries.nix { inherit lib; };
  defaultPackage = pkgs.callPackage ../default.nix { };
  stateRoot = "/var/lib/imladris/terracompute-ops";
  incidentsRoot = "${stateRoot}/incidents";
  sanitizedRoot = "${stateRoot}/sanitized-evidence";
  backupRoot = "${stateRoot}/backups";
  backupTrigger = "${stateRoot}/backup-expedited.trigger";
  backupPreflightRoot = "/run/terracompute-backup-preflight";
  backupPreflightIncomingRoot = "${backupPreflightRoot}/incoming";
  backupPreflightIncomingPath = "${backupPreflightIncomingRoot}/pelargir-preflight.json";
  backupPreflightPublicationRoot = "${backupPreflightRoot}/published";
  watchdogRoot = "/var/lib/terracompute-watchdog";
  actionsRoot = "/var/lib/terracompute-actions";
  investigatorHome = "/var/lib/imladris/terracompute-codex";
  # Codex reads this beside its auth. A permissions profile that extends nothing
  # grants nothing, so no command a model asks for can run at all -- which costs a
  # reasoning turn nothing and is a plainer guarantee than listing readable roots.
  # Without it the turn sandbox is read-only over the whole filesystem, including
  # the directory holding auth.json.
  investigatorCodexConfig = pkgs.writeText "codex-config.toml" ''
    # Top-level keys must precede every table: a bare key after a table header
    # belongs to that table, so this sat under permissions.sealed.network and did
    # nothing at all.
    #
    # This runtime asks a model to read evidence and answer one JSON object. It has
    # no use for MCP tools, so their startup cost is paid for nothing and their tool
    # surface is exactly what the sealed profile below exists to deny.
    mcp_servers = {}
    default_permissions = "sealed"
    # Web search runs on the provider's side and is independent of the sealed network
    # below. The agent needs it to check the upstream status of what we installed.
    # thread/start sets the same value; this is the default for anything that does not.
    web_search = "live"

    # The agent's only view of the GPU host is the reads the actions service runs for
    # it. Codex's own shell would act on this controller, which the agent mistook for
    # the host on 2026-09-24. The code-mode host stays on, because web search runs
    # through it. thread/start sets the same.
    [features]
    shell_tool = false
    unified_exec = false

    [permissions.sealed]

    [permissions.sealed.fileSystem]
    entries = []

    [permissions.sealed.network]
    enabled = false
  '';
  investigatorRoot = "/var/lib/terracompute-investigator";
  investigatorRequests = "${investigatorRoot}/requests";
  investigatorResults = "${investigatorRoot}/results";
  investigatorDatabase = "${investigatorRoot}/database";
  backupAttestation = "backup-v2-pelargir-receiver-and-quota-probe-verified";
  backupPreflightPath = "${backupPreflightPublicationRoot}/pelargir-preflight.json";
  watchdogAttestation = "watchdog-v2-local-heartbeat-and-healthchecks-verified";
  investigatorAttestation = "investigator-v2-linux-arm64-isolation-auth-seeding-and-named-producer-verified";
  capabilityBrokerAttestation = "capability-broker-v2-fail-closed-run-request-ledger-and-typed-effects-verified";
  sandboxRunnerAttestation = "sandbox-runner-v2-external-process-no-network-task-root-only-no-credential-paths-kill-terminates-all-work-verified";
  actionsAttestation = "actions-v1-monitor-restart-actor-telegram-and-live-dry-check-verified";
  requiredPath = name: value:
    if value == null then "/invalid/missing-${name}" else toString value;
  requiredPackage = name: value:
    if value == null then pkgs.writeShellScriptBin "missing-${name}" "exit 126" else value;
  backupCommissioned =
    cfg.backup.configFile != null
    && cfg.backup.preflightAttestationFile != null
    && toString cfg.backup.preflightAttestationFile == backupPreflightPath
    && cfg.backup.commissioningAttestation == backupAttestation
    && boundaries.credentialsExact boundaries.backupCredentialNames cfg.backup.credentials;
  watchdogCommissioned =
    cfg.watchdog.configFile != null
    && cfg.watchdog.commissioningAttestation == watchdogAttestation
    && boundaries.credentialsExact boundaries.watchdogCredentialNames cfg.watchdog.credentials;
  investigatorCommissioned =
    cfg.investigator.configFile != null
    && cfg.investigator.codexPackage != null
    && cfg.investigator.commissioningAttestation == investigatorAttestation
    && cfg.investigator.runtimeVersion != null
    && cfg.investigator.runtimeVersion == cfg.investigator.approvedRuntimeVersion
    && cfg.investigator.runtimeClosureHash != null
    && cfg.investigator.runtimeClosureHash == cfg.investigator.approvedRuntimeClosureHash;
  actionsCommissioned =
    cfg.actions.configFile != null
    && cfg.actions.commissioningAttestation == actionsAttestation
    && boundaries.credentialsExact boundaries.actionsCredentialNames cfg.actions.credentials;
  coreEnabled = lib.any (value: value) [
    cfg.collector.enable cfg.notifier.enable cfg.operatorInput.enable
    cfg.webhook.enable cfg.backup.enable cfg.watchdog.enable
  ];

  credentialAssertions = roleName: role: allowed: [
    {
      assertion = !role.enable || role.configFile != null;
      message = "terracompute-ops ${roleName} requires an operator-provided nonsecret configFile";
    }
    {
      assertion = !role.enable || boundaries.credentialsAllowed allowed role.credentials;
      message = "terracompute-ops ${roleName} credentials may only be: ${lib.concatStringsSep ", " allowed}";
    }
  ];
  exactCredentialAssertions = roleName: role: required: [
    {
      assertion = !role.enable || role.configFile != null;
      message = "terracompute-ops ${roleName} requires an operator-provided nonsecret configFile";
    }
    {
      assertion = !role.enable || boundaries.credentialsExact required role.credentials;
      message = "terracompute-ops ${roleName} requires exactly these credentials: ${lib.concatStringsSep ", " required}";
    }
  ];
  roleOptions = description: {
    enable = lib.mkEnableOption description;
    configFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/etc/terracompute-ops/${description}.json";
      description = "Operator-provided nonsecret strict JSON configuration.";
    };
    credentials = lib.mkOption {
      type = lib.types.attrsOf lib.types.path;
      default = { };
      description = "Host paths loaded into this role's private systemd credential directory.";
    };
    memoryMaxBytes = lib.mkOption {
      type = lib.types.ints.between (64 * 1024 * 1024) (2 * 1024 * 1024 * 1024);
      default = 256 * 1024 * 1024;
    };
    tasksMax = lib.mkOption {
      type = lib.types.ints.between 8 256;
      default = 32;
    };
  };
  commissionedRoleOptions = description: roleOptions description // {
    commissioningAttestation = lib.mkOption {
      type = lib.types.nullOr lib.types.nonEmptyStr;
      default = null;
      description = "Exact reviewed commissioning attestation; arbitrary text is not accepted.";
    };
  };
  mkCoreService = { description, roleName, role, user, networkMode }:
    {
      inherit description;
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      unitConfig.RequiresMountsFor = [ stateRoot ];
      serviceConfig = boundaries.mkServiceConfig {
        inherit user networkMode;
        group = boundaries.sharedGroup;
        memoryMaxBytes = role.memoryMaxBytes;
        tasksMax = role.tasksMax;
        readWritePaths = [ stateRoot ];
      } // {
        Type = "simple";
        ExecStart = "${cfg.package}/bin/terracompute-ops ${roleName} --config ${lib.escapeShellArg (requiredPath "${roleName}-config" role.configFile)}";
        LoadCredential = boundaries.credentialLoads role.credentials;
        Restart = "on-failure";
        RestartSec = "10s";
        TimeoutStartSec = "60s";
        TimeoutStopSec = "45s";
        KillMode = "mixed";
      };
    };
in
{
  options.services.terracomputeOps = {
    enable = lib.mkEnableOption "reusable Terracompute observation service definitions";
    package = lib.mkOption {
      type = lib.types.package;
      default = defaultPackage;
      defaultText = lib.literalExpression "pkgs.callPackage ../default.nix { }";
      description = "Repository package providing every Terracompute runtime entrypoint.";
    };
    observationOnly = lib.mkOption { type = lib.types.bool; default = true; readOnly = true; };
    targetMachineId = lib.mkOption { type = lib.types.str; default = "17049"; readOnly = true; };
    stateRoot = lib.mkOption { type = lib.types.path; default = stateRoot; readOnly = true; };
    collector = lib.recursiveUpdate (roleOptions "collector") {
      memoryMaxBytes.default = 768 * 1024 * 1024;
      tasksMax.default = 96;
    };
    notifier = roleOptions "notifier";
    operatorInput = roleOptions "operator-input";
    webhook = roleOptions "webhook";
    backup = lib.recursiveUpdate (commissionedRoleOptions "hourly protected-state backup") {
      preflightAttestationFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "Separately supplied, expiring Pelargir receiver quota/free-space attestation JSON.";
      };
      resticPackage = lib.mkOption { type = lib.types.package; default = pkgs.restic; };
      opensshPackage = lib.mkOption { type = lib.types.package; default = pkgs.openssh; };
      tasksMax.default = 24;
    };
    watchdog = lib.recursiveUpdate (commissionedRoleOptions "Healthchecks.io controller dead-man switch") {
      tasksMax.default = 24;
    };
    # The one approval-gated action path: restarting dcgm-exporter to release a blocked
    # GPU handover. It never acts without an exact Telegram approval per proposal.
    actions = lib.recursiveUpdate (commissionedRoleOptions "approval-gated monitoring restart") {
      opensshPackage = lib.mkOption { type = lib.types.package; default = pkgs.openssh; };
      tasksMax.default = 24;
    };
    investigator = {
      enable = lib.mkEnableOption "standalone Codex App Server investigator";
      configFile = lib.mkOption { type = lib.types.nullOr lib.types.path; default = null; };
      credentials = lib.mkOption {
        type = lib.types.attrsOf lib.types.path;
        default = { };
        readOnly = true;
        description = "Always empty; auth remains under the ordinary private HOME.";
      };
      commissioningAttestation = lib.mkOption { type = lib.types.nullOr lib.types.nonEmptyStr; default = null; };
      codexPackage = lib.mkOption {
        type = lib.types.nullOr lib.types.package;
        default = null;
        description = "Hash-pinned package whose only child argv is bin/codex app-server.";
      };
      runtimeVersion = lib.mkOption { type = lib.types.nullOr lib.types.nonEmptyStr; default = null; };
      approvedRuntimeVersion = lib.mkOption { type = lib.types.nullOr lib.types.nonEmptyStr; default = null; };
      runtimeClosureHash = lib.mkOption {
        type = lib.types.nullOr (lib.types.strMatching "sha256-[A-Za-z0-9+/=]{20,}"); default = null;
      };
      approvedRuntimeClosureHash = lib.mkOption {
        type = lib.types.nullOr (lib.types.strMatching "sha256-[A-Za-z0-9+/=]{20,}"); default = null;
      };
      collectorIngress = lib.mkOption {
        type = lib.types.bool;
        default = false;
        readOnly = true;
        description = "Uncommissioned; this module grants no collector-to-investigator bridge.";
      };
      actionsIngress = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = ''
          Whether the approval-gated action service may ask this investigator what is
          wrong. It becomes the runtime's one named producer: it may publish a request
          in the pending spool and read and consume its own answer in the completed
          spool, through a group that exists for nothing else. Claimed work, the
          quarantine, the investigator's database and its HOME stay out of reach, and
          an answer can only ever become a finding against the action catalogue.
        '';
      };
      memoryMaxBytes = lib.mkOption {
        type = lib.types.ints.between (256 * 1024 * 1024) (2 * 1024 * 1024 * 1024);
        default = 1024 * 1024 * 1024;
      };
      tasksMax = lib.mkOption { type = lib.types.ints.between 16 256; default = 96; };
      capabilityBroker = {
        enable = lib.mkEnableOption ''
          the Phase 2 capability-broker foundation. Off by default and inert when
          off: nothing is exported to the investigator unit and the runtime builds
          no broker. Turning it on only sets the broker feature-flag environment;
          the broker itself still holds no credential and reaches the target only
          through the existing read-only observe path. Enabling the foundation
          does not create a workspace sandbox: workspace run stays fail-closed in
          the runtime unless a separately attested sandbox runner is configured
          here and injected there
        '';
        commissioningAttestation = lib.mkOption {
          type = lib.types.nullOr lib.types.nonEmptyStr;
          default = null;
        };
        workspaceRoot = lib.mkOption {
          type = lib.types.path;
          default = "${investigatorHome}/task-workspaces";
          description = "The one allowed root for task development worktrees.";
        };
        sandboxRunnerPackage = lib.mkOption {
          type = lib.types.nullOr lib.types.package;
          default = null;
          description = ''
            The isolation runner the broker's workspace run capability is allowed
            to use. It must dispatch a command as an external process inside the
            bound task worktree with no network, no reads or writes outside that
            root, and no credential paths, and hand the broker a handle whose
            kill terminates the process and everything it spawned; the broker
            enforces the deadline against that handle. Null means no runner
            exists and workspace run fails closed.
          '';
        };
        sandboxRunnerAttestation = lib.mkOption {
          type = lib.types.nullOr lib.types.nonEmptyStr;
          default = null;
          description = ''
            The exact attestation string recorded after the sandbox runner's
            isolation contract (external process, no network, task-root-only
            IO, no credential paths, kill terminates all work) has been
            verified on this host. Enabling the broker requires the exact
            current string; a stale earlier-contract attestation fails closed.
          '';
        };
      };
    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    {
      assertions = [
        { assertion = cfg.observationOnly; message = "terracompute-ops must remain observation-only"; }
        { assertion = cfg.targetMachineId == "17049"; message = "terracompute-ops may only observe Vast.ai machine 17049"; }
        {
          assertion = !cfg.backup.enable || backupCommissioned;
          message = "backup requires its exact commissioning string, config, fixed expiring Pelargir quota preflight, restic password, SSH identity, and pinned host keys";
        }
        {
          assertion = !cfg.actions.enable || actionsCommissioned;
          message = "actions require their exact commissioning string, config, Telegram token, actor SSH identity, and pinned actor host key";
        }
        {
          assertion = !cfg.actions.enable || (cfg.collector.enable && cfg.backup.enable);
          message = "actions require the collector (incident evidence) and backup (pre-action evidence preservation)";
        }
        {
          assertion = !(cfg.actions.enable && cfg.operatorInput.enable);
          message = "actions and operator input must not both consume Telegram getUpdates for the same bot";
        }
        {
          assertion = !cfg.watchdog.enable || watchdogCommissioned;
          message = "watchdog requires its exact local-heartbeat/Healthchecks attestation, config, and ping URL";
        }
        {
          assertion = !cfg.investigator.enable || investigatorCommissioned;
          message = "investigator requires exact commissioning metadata and matching measured/approved runtime metadata";
        }
        {
          assertion = !cfg.investigator.collectorIngress;
          message = "collector-to-investigator ingress remains uncommissioned; shared raw state or auth access is forbidden";
        }
        {
          assertion = !cfg.investigator.actionsIngress
            || (cfg.investigator.enable && investigatorCommissioned && cfg.actions.enable);
          message = "action-to-investigator ingress requires both services commissioned and enabled";
        }
        { assertion = cfg.investigator.credentials == { }; message = "investigator must receive no systemd credentials"; }
        {
          assertion = !cfg.investigator.capabilityBroker.enable
            || (cfg.investigator.enable && investigatorCommissioned
                && cfg.investigator.capabilityBroker.commissioningAttestation == capabilityBrokerAttestation
                && cfg.investigator.capabilityBroker.sandboxRunnerPackage != null
                && cfg.investigator.capabilityBroker.sandboxRunnerAttestation == sandboxRunnerAttestation);
          message = "the capability broker requires a commissioned investigator, its own exact commissioning string, and an attested sandbox runner package with the exact isolation-contract string";
        }
      ]
      ++ credentialAssertions "collector" cfg.collector boundaries.collectorCredentialNames
      ++ credentialAssertions "notifier" cfg.notifier boundaries.notifierCredentialNames
      ++ exactCredentialAssertions "operator-input" cfg.operatorInput boundaries.operatorInputCredentialNames
      ++ credentialAssertions "webhook" cfg.webhook boundaries.webhookCredentialNames;
    }
    (lib.mkIf coreEnabled {
      users.groups.${boundaries.sharedGroup} = { };
      users.groups.${boundaries.evidenceGroup} = { };
      systemd.tmpfiles.rules = [
        "d /var/lib/imladris 0755 root root - -"
        "d ${stateRoot} 2771 root ${boundaries.sharedGroup} - -"
        "d ${incidentsRoot} 2770 root ${boundaries.sharedGroup} - -"
        "d ${sanitizedRoot} 2770 root ${boundaries.evidenceGroup} - -"
      ];
      users.users = {
        ${boundaries.collectorUser} = { isSystemUser = true; group = boundaries.sharedGroup; extraGroups = [ boundaries.evidenceGroup ]; };
        ${boundaries.notifierUser} = { isSystemUser = true; group = boundaries.sharedGroup; };
        ${boundaries.operatorInputUser} = { isSystemUser = true; group = boundaries.sharedGroup; };
        ${boundaries.webhookUser} = { isSystemUser = true; group = boundaries.sharedGroup; };
      };
    })
    (lib.mkIf cfg.collector.enable {
      systemd.services.terracompute-collector = mkCoreService {
        description = "Terracompute bounded observation collector for machine 17049";
        roleName = "daemon"; role = cfg.collector; user = boundaries.collectorUser; networkMode = "outbound";
      };
    })
    (lib.mkIf cfg.notifier.enable {
      systemd.services.terracompute-notifier = mkCoreService {
        description = "Terracompute bounded notification outbox worker";
        roleName = "notify"; role = cfg.notifier; user = boundaries.notifierUser; networkMode = "outbound";
      };
    })
    (lib.mkIf cfg.operatorInput.enable {
      systemd.services.terracompute-operator-input = mkCoreService {
        description = "Terracompute authenticated Telegram operator input";
        roleName = "operator-input"; role = cfg.operatorInput; user = boundaries.operatorInputUser; networkMode = "outbound";
      };
    })
    (lib.mkIf cfg.webhook.enable {
      systemd.services.terracompute-webhook = mkCoreService {
        description = "Terracompute loopback-only signed Vast webhook receiver";
        roleName = "webhook"; role = cfg.webhook; user = boundaries.webhookUser; networkMode = "loopback";
      };
    })

    (lib.mkIf (cfg.backup.enable && backupCommissioned) {
      users.groups.${boundaries.backupGroup} = { };
      users.groups.${boundaries.preflightGroup} = { };
      users.users.${boundaries.backupUser} = {
        isSystemUser = true; group = boundaries.backupGroup; extraGroups = [ boundaries.sharedGroup ];
      };
      users.users.${boundaries.preflightUser} = {
        isSystemUser = true; group = boundaries.preflightGroup;
      };
      systemd.tmpfiles.rules = [
        "d ${backupRoot} 0700 ${boundaries.backupUser} ${boundaries.backupGroup} - -"
        "d ${backupPreflightRoot} 0755 root root - -"
        "d ${backupPreflightIncomingRoot} 0750 ${boundaries.preflightUser} ${boundaries.preflightGroup} - -"
        "d ${backupPreflightPublicationRoot} 0750 root ${boundaries.backupGroup} - -"
      ];
      systemd.services.terracompute-backup-preflight-fetch = {
        description = "Fetch fixed Pelargir backup preflight attestation";
        after = [ "network-online.target" ]; wants = [ "network-online.target" ];
        before = [ "terracompute-backup-preflight-publish.service" "terracompute-backup.service" ];
        serviceConfig = boundaries.mkServiceConfig {
          user = boundaries.preflightUser; group = boundaries.preflightGroup; networkMode = "outbound";
          memoryMaxBytes = cfg.backup.memoryMaxBytes; tasksMax = cfg.backup.tasksMax;
          readOnlyPaths = [ "/" ]; readWritePaths = [ backupPreflightIncomingRoot ];
        } // {
          # The root publisher has no DAC override capability and reads the
          # fetched handoff through the dedicated preflight group.
          Type = "oneshot"; UMask = "0027";
          ExecStart = "${cfg.package}/bin/terracompute-backup-preflight fetch --config ${lib.escapeShellArg (toString cfg.backup.configFile)} --incoming ${backupPreflightIncomingPath} --sftp-executable ${cfg.backup.opensshPackage}/bin/sftp --ssh-identity-file %d/ssh-identity --ssh-known-hosts-file %d/known-hosts";
          LoadCredential = boundaries.credentialLoads (
            lib.filterAttrs (name: _: builtins.elem name boundaries.preflightCredentialNames) cfg.backup.credentials
          );
          TimeoutStartSec = "45s";
        };
      };
      systemd.services.terracompute-backup-preflight-publish = {
        description = "Validate and publish Pelargir backup preflight attestation";
        # Wants (rather than Requires) makes the publisher run and clear an old
        # publication even when fetch fails.  Backup Requires both units below.
        wants = [ "terracompute-backup-preflight-fetch.service" ];
        after = [ "terracompute-backup-preflight-fetch.service" ];
        before = [ "terracompute-backup.service" ];
        serviceConfig = boundaries.mkServiceConfig {
          user = "root"; group = boundaries.backupGroup; networkMode = "none";
          memoryMaxBytes = cfg.backup.memoryMaxBytes; tasksMax = cfg.backup.tasksMax;
          readOnlyPaths = [ "/" backupPreflightIncomingRoot ];
          readWritePaths = [ backupPreflightPublicationRoot ];
        } // {
          Type = "oneshot"; UMask = "0027";
          SupplementaryGroups = [ boundaries.preflightGroup ];
          ExecStart = "${cfg.package}/bin/terracompute-backup-preflight publish --config ${lib.escapeShellArg (toString cfg.backup.configFile)} --incoming ${backupPreflightIncomingPath} --publication ${backupPreflightPath}";
          LoadCredential = [ ];
          InaccessiblePaths = map toString (builtins.attrValues cfg.backup.credentials);
          TimeoutStartSec = "15s";
        };
      };
      systemd.services.terracompute-backup = {
        description = "Terracompute hourly protected-state backup";
        requires = [
          "terracompute-backup-preflight-fetch.service"
          "terracompute-backup-preflight-publish.service"
        ];
        after = [
          "network-online.target"
          "terracompute-backup-preflight-fetch.service"
          "terracompute-backup-preflight-publish.service"
        ];
        wants = [ "network-online.target" ];
        unitConfig.RequiresMountsFor = [ stateRoot backupRoot ];
        serviceConfig = boundaries.mkServiceConfig {
          user = boundaries.backupUser; group = boundaries.backupGroup; networkMode = "outbound";
          memoryMaxBytes = cfg.backup.memoryMaxBytes; tasksMax = cfg.backup.tasksMax;
          readOnlyPaths = [ stateRoot backupPreflightPublicationRoot ]; readWritePaths = [ backupRoot ];
        } // {
          Type = "oneshot"; UMask = "0077";
          ExecStart = "${cfg.package}/bin/terracompute-backup --config ${lib.escapeShellArg (toString cfg.backup.configFile)} --preflight-attestation ${lib.escapeShellArg (toString cfg.backup.preflightAttestationFile)} --restic-executable ${cfg.backup.resticPackage}/bin/restic --restic-password-file %d/restic-password --ssh-executable ${cfg.backup.opensshPackage}/bin/ssh --ssh-identity-file %d/ssh-identity --ssh-known-hosts-file %d/known-hosts";
          LoadCredential = boundaries.credentialLoads cfg.backup.credentials;
          TimeoutStartSec = "15min";
        };
      };
      systemd.timers.terracompute-backup = {
        wantedBy = [ "timers.target" ];
        timerConfig = { OnCalendar = "hourly"; AccuracySec = "1min"; Persistent = true; Unit = "terracompute-backup.service"; };
      };
      # Multiple path activations targeting the same oneshot are coalesced by systemd.
      systemd.paths.terracompute-backup-expedited = {
        wantedBy = [ "paths.target" ];
        pathConfig = { PathChanged = backupTrigger; Unit = "terracompute-backup.service"; };
      };
    })

    (lib.mkIf (cfg.actions.enable && actionsCommissioned) {
      users.groups.${boundaries.actionsGroup} = { };
      users.users.${boundaries.actionsUser} = {
        isSystemUser = true; group = boundaries.actionsGroup;
        extraGroups = [ boundaries.sharedGroup ]
          ++ lib.optional cfg.investigator.actionsIngress boundaries.investigatorBridgeGroup;
      };
      # Approval, nonce, lock, attempt and inbox state; no other role can write it.
      systemd.tmpfiles.rules = [
        "d ${actionsRoot} 0700 ${boundaries.actionsUser} ${boundaries.actionsGroup} - -"
      ];
      # The local operator console: ask the agent a question and read the conversation
      # back. Runs as the service user, so it cannot leave root-owned WAL files behind,
      # and needs sudo, so only root can file a question. It files questions only.
      environment.systemPackages = [
        (pkgs.writeShellScriptBin "terracompute-console" ''
          exec /run/wrappers/bin/sudo -u ${boundaries.actionsUser} \
            ${cfg.package}/bin/terracompute-console \
            --config ${lib.escapeShellArg (toString cfg.actions.configFile)} "$@"
        '')
      ];
      systemd.services.terracompute-actions = {
        description = "Terracompute approval-gated monitoring restart for blocked GPU handovers";
        wantedBy = [ "multi-user.target" ];
        after = [ "network-online.target" "terracompute-collector.service" ];
        wants = [ "network-online.target" ];
        unitConfig.RequiresMountsFor = [ stateRoot actionsRoot ];
        serviceConfig = boundaries.mkServiceConfig {
          user = boundaries.actionsUser; group = boundaries.actionsGroup; networkMode = "outbound";
          memoryMaxBytes = cfg.actions.memoryMaxBytes; tasksMax = cfg.actions.tasksMax;
          readWritePaths = [ stateRoot actionsRoot ]
            # Publishing a question and consuming its answer are both writes; the
            # filesystem modes above are what actually bound them.
            ++ lib.optionals cfg.investigator.actionsIngress [
              # One bind mount covering staging and pending, so publishing a request
              # is a rename and never a copy. The directory modes are what actually
              # bound this: the requests root itself grants the group no write.
              investigatorRequests
              "${investigatorResults}/completed"
            ];
        } // {
          # Evidence and the backup trigger stay in the shared, backed-up state root.
          SupplementaryGroups = [ boundaries.sharedGroup ];
          Type = "simple";
          ExecStart = "${cfg.package}/bin/terracompute-actions --config ${lib.escapeShellArg (toString cfg.actions.configFile)} --telegram-token-file %d/telegram-token --actor-identity-file %d/actor-ssh-identity --actor-known-hosts-file %d/actor-known-hosts --ssh-executable ${cfg.actions.opensshPackage}/bin/ssh --systemctl-executable ${pkgs.systemd}/bin/systemctl";
          LoadCredential = boundaries.credentialLoads cfg.actions.credentials;
          Restart = "on-failure";
          RestartSec = "30s";
          TimeoutStartSec = "60s";
          # Longer than one restart dispatch; an interrupted dispatch is reconciled, never replayed.
          TimeoutStopSec = "300s";
          KillMode = "mixed";
        };
      };
    })

    (lib.mkIf (cfg.watchdog.enable && watchdogCommissioned) {
      users.groups.${boundaries.watchdogGroup} = { };
      users.users.${boundaries.watchdogUser} = {
        isSystemUser = true;
        group = boundaries.watchdogGroup;
        extraGroups = [ boundaries.sharedGroup ];
      };
      systemd.tmpfiles.rules = [
        "d ${watchdogRoot} 0700 ${boundaries.watchdogUser} ${boundaries.watchdogGroup} - -"
        "d ${watchdogRoot}/state 0700 ${boundaries.watchdogUser} ${boundaries.watchdogGroup} - -"
      ];
      systemd.services.terracompute-watchdog = {
        description = "Terracompute Healthchecks.io controller watchdog";
        after = [ "network-online.target" ]; wants = [ "network-online.target" ];
        serviceConfig = boundaries.mkServiceConfig {
          user = boundaries.watchdogUser; group = boundaries.watchdogGroup; networkMode = "outbound";
          memoryMaxBytes = cfg.watchdog.memoryMaxBytes; tasksMax = cfg.watchdog.tasksMax;
          readOnlyPaths = [ stateRoot ]; readWritePaths = [ watchdogRoot ];
        } // {
          Type = "oneshot"; UMask = "0077";
          ExecStart = "${cfg.package}/bin/terracompute-watchdog --config ${lib.escapeShellArg (toString cfg.watchdog.configFile)} --healthchecks-ping-url-file %d/healthchecks-ping-url";
          LoadCredential = boundaries.credentialLoads cfg.watchdog.credentials;
          TimeoutStartSec = "45s";
        };
      };
      systemd.timers.terracompute-watchdog = {
        wantedBy = [ "timers.target" ];
        timerConfig = { OnBootSec = "30s"; OnUnitActiveSec = "30s"; AccuracySec = "1s"; Persistent = false; Unit = "terracompute-watchdog.service"; };
      };
    })

    (lib.mkIf (cfg.investigator.enable && investigatorCommissioned) {
      users.groups.${boundaries.investigatorGroup} = { };
      users.users.${boundaries.investigatorUser} = {
        isSystemUser = true; group = boundaries.investigatorGroup; home = investigatorHome;
        createHome = false;
        # A producer's request is owned by the producer, so reading it needs the group
        # they share. The bridge group is exactly these two services and nothing else.
        extraGroups = lib.optional cfg.investigator.actionsIngress boundaries.investigatorBridgeGroup;
      };
      users.groups.${boundaries.investigatorBridgeGroup} =
        lib.mkIf cfg.investigator.actionsIngress { };
      systemd.tmpfiles.rules = [
        "d /var/lib/imladris 0755 root root - -"
        "d ${investigatorHome} 0700 ${boundaries.investigatorUser} ${boundaries.investigatorGroup} - -"
        # Codex owns this directory; we place one file in it and never the auth.
        "d ${investigatorHome}/.codex 0700 ${boundaries.investigatorUser} ${boundaries.investigatorGroup} - -"
        "L+ ${investigatorHome}/.codex/config.toml - - - - ${investigatorCodexConfig}"
        # Traverse-only for the bridge when a producer is named: every child is gated
        # on its own, and a private root would put all of them out of reach.
        (
          if cfg.investigator.actionsIngress then
            "d ${investigatorRoot} 0710 ${boundaries.investigatorUser} ${boundaries.investigatorBridgeGroup} - -"
          else
            "d ${investigatorRoot} 0700 ${boundaries.investigatorUser} ${boundaries.investigatorGroup} - -"
        )
        # Claimed work, the quarantine and the database are the runtime's alone,
        # whether or not a producer is named.
        "d ${investigatorRequests}/claimed 0700 ${boundaries.investigatorUser} ${boundaries.investigatorGroup} - -"
        "d ${investigatorResults}/quarantine 0700 ${boundaries.investigatorUser} ${boundaries.investigatorGroup} - -"
        "d ${investigatorDatabase} 0700 ${boundaries.investigatorUser} ${boundaries.investigatorGroup} - -"
        "f ${investigatorDatabase}/investigator.sqlite3 0600 ${boundaries.investigatorUser} ${boundaries.investigatorGroup} - -"
      ]
      ++ (
        if cfg.investigator.actionsIngress then
          # The named producer traverses the two roots, creates a request in pending
          # (sticky: it cannot unlink another's) and reads and consumes its answer in
          # completed (setgid: answers carry the bridge group). Nothing for others.
          [
            "d ${investigatorRequests} 0710 ${boundaries.investigatorUser} ${boundaries.investigatorBridgeGroup} - -"
            # setgid as well as sticky: a request must carry the bridge group, or the
            # runtime could not read a file the producer owns.
            "d ${investigatorRequests}/pending 3730 ${boundaries.investigatorUser} ${boundaries.investigatorBridgeGroup} - -"
            # The producer builds a request here and renames it into pending. It must
            # share a mount with pending: systemd gives each ReadWritePaths entry its
            # own bind mount, and rename(2) is EXDEV across mount points even on one
            # filesystem. The runtime never looks here.
            "d ${investigatorRequests}/staging 2770 ${boundaries.investigatorUser} ${boundaries.investigatorBridgeGroup} - -"
            "d ${investigatorResults} 0710 ${boundaries.investigatorUser} ${boundaries.investigatorBridgeGroup} - -"
            "d ${investigatorResults}/completed 2770 ${boundaries.investigatorUser} ${boundaries.investigatorBridgeGroup} - -"
          ]
        else
          [
            "d ${investigatorRequests} 0700 ${boundaries.investigatorUser} ${boundaries.investigatorGroup} - -"
            "d ${investigatorRequests}/pending 0700 ${boundaries.investigatorUser} ${boundaries.investigatorGroup} - -"
            "d ${investigatorResults} 0700 ${boundaries.investigatorUser} ${boundaries.investigatorGroup} - -"
            "d ${investigatorResults}/completed 0700 ${boundaries.investigatorUser} ${boundaries.investigatorGroup} - -"
          ]
      );
      systemd.services.terracompute-investigator = {
        description = "Commissioned standalone Terracompute Codex investigator";
        wantedBy = [ "multi-user.target" ]; after = [ "network-online.target" ]; wants = [ "network-online.target" ];
        unitConfig.RequiresMountsFor = [ investigatorHome ];
        # With the broker flag off (the default) this is exactly the old
        # environment: nothing about the deployed unit changes.
        environment = { HOME = investigatorHome; }
          // lib.optionalAttrs cfg.investigator.capabilityBroker.enable ({
            TERRACOMPUTE_CAPABILITY_BROKER = "1";
            TERRACOMPUTE_BROKER_WORKSPACE_ROOT = toString cfg.investigator.capabilityBroker.workspaceRoot;
            TERRACOMPUTE_BROKER_STATE_ROOT = "${investigatorRoot}/broker";
          } // lib.optionalAttrs (cfg.investigator.capabilityBroker.sandboxRunnerPackage != null) {
            # Named only when the attested runner exists; the runtime never
            # invents a sandbox from this and run stays fail-closed without it.
            TERRACOMPUTE_BROKER_SANDBOX_RUNNER =
              "${cfg.investigator.capabilityBroker.sandboxRunnerPackage}/bin/sandbox-runner";
          });
        serviceConfig = boundaries.mkServiceConfig {
          user = boundaries.investigatorUser; group = boundaries.investigatorGroup; networkMode = "outbound";
          memoryMaxBytes = cfg.investigator.memoryMaxBytes; tasksMax = cfg.investigator.tasksMax;
          readWritePaths = [ investigatorHome investigatorRoot ];
        } // {
          Type = "simple"; UMask = "0077";
          # Codex 0.154 runs web search through its code-mode host, a JavaScript JIT
          # that needs writable-executable memory. With W^X enforced it dies with
          # SIGTRAP and search goes with it. Measured 2026-09-24 inside this unit's
          # exact sandbox: W^X on, no search; W^X off, three searches and no crash.
          # The JIT runs inside codex, so the exception cannot be narrowed to it.
          #
          # What this gives up: W^X is an exploit mitigation. Without it, a memory-
          # corruption bug in this process is easier to turn into code execution.
          # Such code would still be held by the rest of this unit -- no capabilities,
          # NoNewPrivileges, a read-only system, a private /tmp, no key to the GPU host
          # -- but it would have this unit's outbound network and its home, which
          # holds the codex login. Codex's shell tools are off (config below); that
          # removes the intended way to run commands, not the mitigation.
          # Operator decision, 2026-09-24: web search is required.
          MemoryDenyWriteExecute = false;
          ExecStart = "${cfg.package}/bin/terracompute-investigator --config ${lib.escapeShellArg (toString cfg.investigator.configFile)} --codex-executable ${(requiredPackage "codex" cfg.investigator.codexPackage)}/bin/codex";
          LoadCredential = [ ]; Restart = "always"; RestartSec = "10s";
          TimeoutStopSec = "45s"; KillMode = "mixed";
        };
      };
    })
  ]);
}
