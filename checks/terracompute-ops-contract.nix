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
  sourceTree = lib.removeSuffix "\n" (builtins.readFile (source + "/SOURCE_TREE"));
  expectedSourceTree = "69ce5b65e12542758411286e8d3d5e18629248ea";
  sourceArchive = lib.removeSuffix "\n" (builtins.readFile (source + "/SOURCE_ARCHIVE_SHA256"));
  expectedSourceArchive = "26de99f7da1d5c347cacf48e293ea6db400e36e189f6a0cecad1d40ac4a28ff4";
  manifestHash = builtins.hashFile "sha256" (source + "/SOURCE_MANIFEST.sha256");
  expectedManifestHash = "c530db70e334120854c51fc65a117187ea5fd8dafbde4034fde8889cc532d034";
  cfg = nixosConfigurations.imladris.config;
  ops = cfg.services.terracomputeOps;
  disabledTerracomputeSecrets = lib.filter (name: lib.hasPrefix "terracompute-" name) (
    builtins.attrNames cfg.sops.secrets
  );
  hostSource = builtins.readFile ../hosts/nixos/imladris/terracompute-ops.nix;
  hostDefaultSource = builtins.readFile ../hosts/nixos/imladris/default.nix;
  l2tpSource = builtins.readFile ../hosts/nixos/imladris/terracompute-l2tp.nix;
  moduleSource = builtins.readFile (source + "/nix/nixos-module.nix");
  package = pkgs.callPackage (source + "/default.nix") { };
  requiredHostFragments = [
    expectedRevision
    "enable = false;"
    ''target = "terracompute-observer@10.50.0.2";''
    ''endpoint = "http://10.50.0.2:9090";''
    ''repository = "sftp:terracompute-backup@pelargir:/terracompute-ops";''
    "group_id = -1004484415005;"
    "9097629B14F1AD21A5959AE9BB77524E05D8EA3C3FD0F32A92A139EEB9EE1514"
    "terracompute-vast-read-api-key"
    "terracompute-bmc-password"
    "terracompute-healthchecks-ping-url"
  ];
  missing = lib.filter (fragment: !lib.hasInfix fragment hostSource) requiredHostFragments;
  requiredL2tpFragments = [
    ''"10.50.0.2/32"''
    ''"10.0.15.237/32"''
    "nodefaultroute"
    "nodefaultroute6"
    "noresolvconf"
    ''Type = "notify";''
    ''TimeoutStartSec = "75s";''
    ''requires = [ "terracompute-l2tp.service" ];''
    ''NIX_REDIRECTS "/var/run=/run/pppd"''
  ];
  missingL2tp = lib.filter (fragment: !lib.hasInfix fragment l2tpSource) requiredL2tpFragments;
in
if
  revision != expectedRevision
  || sourceTree != expectedSourceTree
  || sourceArchive != expectedSourceArchive
  || manifestHash != expectedManifestHash
then
  throw "terracompute vendor provenance does not match the reviewed standalone commit"
else if ops.enable || !ops.observationOnly || ops.targetMachineId != "17049" then
  throw "terracompute must remain disabled, observation-only and scoped to machine 17049"
else if disabledTerracomputeSecrets != [ ] then
  throw "disabled terracompute integration must not materialize runtime secrets"
else if
  builtins.hasAttr "terracompute-l2tp" cfg.systemd.services
  || builtins.hasAttr "terracompute-l2tp-route-guards" cfg.systemd.services
then
  throw "disabled terracompute integration must not materialize L2TP services or route guards"
else if missing != [ ] then
  throw "terracompute Imladris commissioning configuration is incomplete"
else if !lib.hasInfix "./terracompute-l2tp.nix" hostDefaultSource || missingL2tp != [ ] then
  throw "terracompute L2TP commissioning contract is incomplete"
else if !lib.hasInfix ''cfg.targetMachineId == "17049"'' moduleSource then
  throw "terracompute module lost its fixed machine identity assertion"
else if !pkgs.stdenv.hostPlatform.isLinux then
  pkgs.runCommand "terracompute-ops-contract-ok" { nativeBuildInputs = [ pkgs.coreutils ]; } ''
    cd ${source}
    sha256sum --check SOURCE_MANIFEST.sha256
    touch "$out"
  ''
else
  pkgs.runCommand "terracompute-ops-contract-ok"
    {
      inherit package;
      nativeBuildInputs = [ pkgs.coreutils ];
    }
    ''
      cd ${source}
      sha256sum --check SOURCE_MANIFEST.sha256
      test -x "${package}/bin/terracompute-ops"
      test -x "${package}/bin/terracompute-backup"
      test -x "${package}/bin/terracompute-watchdog"
      touch "$out"
    ''
