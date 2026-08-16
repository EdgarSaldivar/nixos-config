{ lib, pkgs, nixosConfigurations, darwinConfigurations, ... }:

# Tang is intentionally a narrow LAN-only boot dependency, and its key
# directory must enter Pelargir's existing off-host restic recovery set.
  let
    cfg = nixosConfigurations.pelargir.config;
    tang = cfg.services.tang;
    tangService = cfg.systemd.services."tangd@".serviceConfig;
    restic = cfg.systemd.services.restic-backups-minas;
  in
  if
    !tang.enable
    || tang.listenStream != [ "0.0.0.0:7654" ]
    ||
      tang.ipAddressAllow != [
        "127.0.0.1/32"
        "10.0.0.118/32"
      ]
  then
    throw "pelargir Tang listener or systemd source ACL changed"
  else if tangService.RuntimeDirectoryPreserve != true then
    throw "pelargir Tang must preserve its shared runtime directory across Accept=yes instances"
  else if
    !lib.hasInfix ''iifname "eth0" ip saddr 10.0.0.118 tcp dport 7654 accept'' cfg.networking.firewall.extraInputRules
  then
    throw "pelargir Tang lost its source-scoped nftables rule"
  else if
    !lib.elem "pelargir-stage-tang-state.service" restic.requires
    || !lib.elem "pelargir-stage-tang-state.service" restic.after
  then
    throw "pelargir restic no longer requires the Tang key staging service"
  else
    pkgs.runCommand "pelargir-tang-contract-ok" { } "touch $out"
