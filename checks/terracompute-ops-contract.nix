{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:
let
  source = ../vendor/terracompute-ops;
  revision = lib.removeSuffix "\n" (builtins.readFile (source + "/SOURCE_REV"));
  expectedRevision = "fe53fe716d2a5a9d5166b51068308ac300a513f3";
  sourceTree = lib.removeSuffix "\n" (builtins.readFile (source + "/SOURCE_TREE"));
  expectedSourceTree = "1b20701c18549ad3882d431dc83edb52b9dd22e9";
  sourceArchive = lib.removeSuffix "\n" (builtins.readFile (source + "/SOURCE_ARCHIVE_SHA256"));
  expectedSourceArchive = "f2831c26b58e766f0a8b23f13e8086357a52d3839d0ce940b50b0a4228cc86e8";
  manifestHash = builtins.hashFile "sha256" (source + "/SOURCE_MANIFEST.sha256");
  expectedManifestHash = "97f98cfceb2a0f1dd0ffb5f4bfe0bc13f988096f99f22266ef99512d28e44e7c";
  cfg = nixosConfigurations.imladris.config;
  ops = cfg.services.terracomputeOps;
  transport = cfg.services.terracomputeL2tp;
  terracomputeSecrets = lib.filter (name: lib.hasPrefix "terracompute-" name) (
    builtins.attrNames cfg.sops.secrets
  );
  expectedControllerSecrets = [
    "terracompute-bmc-password"
    "terracompute-healthchecks-ping-url"
    "terracompute-known-hosts"
    "terracompute-l2tp-ipsec-psk"
    "terracompute-l2tp-password"
    "terracompute-l2tp-server"
    "terracompute-l2tp-username"
    "terracompute-ssh-identity"
    "terracompute-telegram-bot-token"
    "terracompute-telegram-chat-id"
    "terracompute-vast-read-api-key"
  ];
  hostSource = builtins.readFile ../hosts/nixos/imladris/terracompute-ops.nix;
  hostDefaultSource = builtins.readFile ../hosts/nixos/imladris/default.nix;
  l2tpSource = builtins.readFile ../hosts/nixos/imladris/terracompute-l2tp.nix;
  moduleSource = builtins.readFile (source + "/nix/nixos-module.nix");
  package = pkgs.callPackage (source + "/default.nix") { };
  requiredHostFragments = [
    expectedRevision
    "enable = true;"
    "services.terracomputeL2tp.enable = true;"
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
    "systemd-notify --ready"
    ''NotifyAccess = "all";''
    "ReadWritePaths = [ runtimeDirectory ];"
    ''"AF_PACKET"''
    ''writeShellScript "terracompute-l2tp-route-guards-stop"''
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
else if
  !ops.enable || !transport.enable || !ops.observationOnly || ops.targetMachineId != "17049"
then
  throw "terracompute observation commissioning must remain observation-only on machine 17049"
else if terracomputeSecrets != expectedControllerSecrets then
  throw "terracompute observation commissioning must materialize exactly the reviewed credentials"
else if
  !builtins.hasAttr "strongswan-swanctl" cfg.systemd.services
  || !builtins.hasAttr "terracompute-l2tp" cfg.systemd.services
  || !builtins.hasAttr "terracompute-l2tp-route-guards" cfg.systemd.services
  || !builtins.hasAttr "terracompute-collector" cfg.systemd.services
  || !builtins.hasAttr "terracompute-notifier" cfg.systemd.services
  || !builtins.hasAttr "terracompute-watchdog" cfg.systemd.services
  || builtins.hasAttr "terracompute-backup" cfg.systemd.services
then
  throw "terracompute observation commissioning service set is incomplete"
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
