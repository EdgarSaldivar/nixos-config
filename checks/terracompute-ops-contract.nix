{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:
let
  source = ../vendor/terracompute-ops;
  revision = lib.removeSuffix "\n" (builtins.readFile (source + "/SOURCE_REV"));
  expectedRevision = "d9434988005784e50eab3114415d5c730ca16efe";
  cfg = nixosConfigurations.imladris.config;
  ops = cfg.services.terracomputeOps;
  hostSource = builtins.readFile ../hosts/nixos/imladris/terracompute-ops.nix;
  moduleSource = builtins.readFile (source + "/nix/nixos-module.nix");
  package = pkgs.callPackage (source + "/default.nix") { };
  requiredHostFragments = [
    expectedRevision
    "enable = false;"
    ''target = "terracompute-observer@10.50.0.2";''
    ''endpoint = "http://10.50.0.2:9090";''
    ''repository = "sftp:terracompute-backup@pelargir:/terracompute-ops";''
    "group_id = -1004484415005;"
    "terracompute-vast-read-api-key"
    "terracompute-bmc-password"
    "terracompute-healthchecks-ping-url"
  ];
  missing = lib.filter (fragment: !lib.hasInfix fragment hostSource) requiredHostFragments;
in
if revision != expectedRevision then
  throw "terracompute vendor revision is not the reviewed standalone commit"
else if ops.enable || !ops.observationOnly || ops.targetMachineId != "17049" then
  throw "terracompute must remain disabled, observation-only and scoped to machine 17049"
else if missing != [ ] then
  throw "terracompute Imladris commissioning configuration is incomplete"
else if !lib.hasInfix ''cfg.targetMachineId == "17049"'' moduleSource then
  throw "terracompute module lost its fixed machine identity assertion"
else if !pkgs.stdenv.hostPlatform.isLinux then
  pkgs.runCommand "terracompute-ops-contract-ok" { } ''touch "$out"''
else
  pkgs.runCommand "terracompute-ops-contract-ok" { inherit package; } ''
    test -x "${package}/bin/terracompute-ops"
    test -x "${package}/bin/terracompute-backup"
    test -x "${package}/bin/terracompute-watchdog"
    touch "$out"
  ''
