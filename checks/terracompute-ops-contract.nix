{
  lib,
  pkgs,
  nixosConfigurations,
  darwinConfigurations,
  ...
}:
let
  cfg = nixosConfigurations.imladris.config;
  ops = cfg.services.terracomputeOps;
  moduleSource = builtins.readFile ../hosts/nixos/imladris/terracompute-ops.nix;
  imladrisImports = builtins.readFile ../hosts/nixos/imladris/default.nix;
  pelargirImports = builtins.readFile ../hosts/nixos/pelargir/default.nix;
  pythonSources = map builtins.readFile [
    ../pkgs/terracompute-ops/src/terracompute_ops/cli.py
    ../pkgs/terracompute-ops/src/terracompute_ops/incidents.py
    ../pkgs/terracompute-ops/src/terracompute_ops/state.py
    ../pkgs/terracompute-ops/src/terracompute_ops/supervisor.py
    ../pkgs/terracompute-ops/src/terracompute_ops/telegram.py
  ];
  clientSource = lib.concatStringsSep "\n" pythonSources;
  cliSource = builtins.head pythonSources;
  testsSource = builtins.readFile ../pkgs/terracompute-ops/tests/test_incidents.py;
  testCount = (lib.length (lib.splitString "    def test_" testsSource)) - 1;
  forbiddenClientVocabulary = [
    "--command"
    "StrictHostKeyChecking=no"
    "StrictHostKeyChecking=accept-new"
    "shell=True"
    "kubectl"
    "systemctl"
    "shutdown"
    "reboot"
  ];
  forbiddenPresent = lib.filter (
    fragment: lib.hasInfix fragment clientSource
  ) forbiddenClientVocabulary;
  requiredModuleFragments = [
    ''default = "17049";''
    ''default = "terracompute-observer@10.50.0.2";''
    ''StateDirectory = "imladris/terracompute-ops";''
    ''RequiresMountsFor = [ "/var/lib/imladris" ];''
    ''ProtectSystem = "strict";''
    "ProtectHome = true;"
    "PrivateTmp = true;"
    "PrivateDevices = true;"
    "NoNewPrivileges = true;"
    ''CapabilityBoundingSet = "";''
    ''TimeoutStartSec = "60s";''
    ''MemoryMax = "256M";''
    "TasksMax = 32;"
    "Persistent = false;"
    "LoadCredential = ["
  ];
  missingModuleFragments = lib.filter (
    fragment: !lib.hasInfix fragment moduleSource
  ) requiredModuleFragments;
in
if !lib.hasInfix "./terracompute-ops.nix" imladrisImports then
  throw "terracompute-ops must be imported by imladris"
else if lib.hasInfix "terracompute-ops" pelargirImports then
  throw "terracompute-ops must never be imported by pelargir"
else if ops.enable || !ops.observationOnly || ops.targetMachineId != "17049" then
  throw "terracompute-ops must remain uncommissioned, observation-only, and pinned to machine 17049"
else if
  !lib.hasInfix "StrictHostKeyChecking=yes" cliSource
  || !lib.hasInfix "UserKnownHostsFile=" cliSource
  || !lib.hasInfix "GlobalKnownHostsFile=/dev/null" cliSource
  || !lib.hasInfix "--known-hosts %d/known-hosts" moduleSource
then
  throw "terracompute-ops requires a pinned SSH host key and four mandatory systemd credentials"
else if missingModuleFragments != [ ] then
  throw "terracompute-ops lost bounded scheduling, dedicated state placement, or systemd hardening"
else if forbiddenPresent != [ ] then
  throw "terracompute-ops exposes generic shell or remote mutation vocabulary: ${lib.concatStringsSep ", " forbiddenPresent}"
else if
  testCount < 10
  || !lib.hasInfix "test_healthy_probe_creates_nothing_and_never_requests_analysis" testsSource
  || !lib.hasInfix "test_duplicate_key_uses_target_boot_family_and_signature" testsSource
  || !lib.hasInfix "test_bundle_manifest_hashes_every_payload_file" testsSource
  || !lib.hasInfix "test_outbox_failure_backoff_retains_pending_item" testsSource
then
  throw "terracompute incident tests are missing or vacuous"
else
  pkgs.runCommand "terracompute-ops-contract-ok"
    {
      nativeBuildInputs = [ pkgs.python3 ];
      packageSource = ../pkgs/terracompute-ops;
    }
    ''
      export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
      export PYTHONPATH="$packageSource/src"
      python -m unittest discover -s "$packageSource/tests" -v
      touch "$out"
    ''
