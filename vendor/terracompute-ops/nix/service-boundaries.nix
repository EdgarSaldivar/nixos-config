{ lib }:
let
  networkConfig = mode:
    if mode == "outbound" then {
      RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" ];
    } else if mode == "loopback" then {
      RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" ];
      IPAddressDeny = "any";
      IPAddressAllow = [ "localhost" ];
    } else if mode == "none" then {
      RestrictAddressFamilies = [ "AF_UNIX" ];
      IPAddressDeny = "any";
      PrivateNetwork = true;
    } else
      throw "unknown terracompute network mode: ${mode}";
in
rec {
  sharedGroup = "terracompute-state";
  evidenceGroup = "terracompute-evidence";
  investigatorGroup = "terracompute-investigator";
  backupGroup = "terracompute-backup";
  preflightGroup = "terracompute-preflight";
  watchdogGroup = "terracompute-watchdog";
  actionsGroup = "terracompute-actions";

  collectorUser = "terracompute-collector";
  notifierUser = "terracompute-notifier";
  operatorInputUser = "terracompute-operator-input";
  webhookUser = "terracompute-webhook";
  backupUser = "terracompute-backup";
  preflightUser = "terracompute-preflight";
  watchdogUser = "terracompute-watchdog";
  investigatorUser = "terracompute-investigator";
  evidenceUser = "terracompute-evidence";
  actionsUser = "terracompute-actions";

  collectorCredentialNames = [
    "ssh-identity"
    "known-hosts"
    "vast-read-api-key"
    "bmc-password"
  ];
  notifierCredentialNames = [
    "telegram-token"
    "telegram-chat-id"
  ];
  operatorInputCredentialNames = [
    "telegram-token"
    "telegram-chat-id"
  ];
  webhookCredentialNames = [ "vast-webhook-secret" ];
  backupCredentialNames = [ "restic-password" "ssh-identity" "known-hosts" ];
  preflightCredentialNames = [ "ssh-identity" "known-hosts" ];
  watchdogCredentialNames = [ "healthchecks-ping-url" ];
  actionsCredentialNames = [ "telegram-token" "actor-ssh-identity" "actor-known-hosts" ];

  credentialsAllowed = allowed: credentials:
    lib.all (name: builtins.elem name allowed) (builtins.attrNames credentials);

  credentialsExact = required: credentials:
    credentialsAllowed required credentials
    && builtins.length (builtins.attrNames credentials) == builtins.length required;

  credentialLoads = credentials:
    lib.mapAttrsToList (name: path: "${name}:${toString path}") credentials;

  # Do not use DynamicUser: the collector, notifier, operator input and webhook are distinct
  # static users whose primary group deliberately owns the shared SQLite/WAL
  # files. RestrictNamespaces is deliberately omitted because the scheduler and
  # runtime use child processes; the remaining controls do not prevent fork,
  # multiprocessing pipes or SQLite WAL/shm files.
  mkServiceConfig = {
    user,
    group,
    networkMode,
    memoryMaxBytes,
    tasksMax,
    readWritePaths ? [ ],
    readOnlyPaths ? [ ],
  }:
    {
      User = user;
      Group = group;
      UMask = "0007";
      MemoryMax = memoryMaxBytes;
      TasksMax = tasksMax;

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
      RestrictRealtime = true;
      RestrictSUIDSGID = true;
      LockPersonality = true;
      MemoryDenyWriteExecute = true;
      CapabilityBoundingSet = "";
      SystemCallArchitectures = "native";
      ReadWritePaths = readWritePaths;
      ReadOnlyPaths = readOnlyPaths;
    }
    // networkConfig networkMode;
}
