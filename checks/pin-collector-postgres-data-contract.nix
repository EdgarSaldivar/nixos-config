{ lib, pkgs, nixosConfigurations, darwinConfigurations, ... }:

# local-path creates the volume root owned by uid 0, and fsGroup sets only the
# group, so a Restricted-Pod-Security container cannot chmod the mount point.
# initdb must own a subdirectory rather than the mount itself, or PostgreSQL
# crash-loops on EPERM the first time the PVC is ever created.
  let
    manifestSource = builtins.readFile ../hosts/nixos/minas-tirith/manifests/pin-collector.yaml.in;
    mountPath = "/var/lib/postgresql/data";
  in
  if
    !lib.hasInfix "{ name: PGDATA, value: ${mountPath}/pgdata }" manifestSource
    || !lib.hasInfix "{ name: data, mountPath: ${mountPath} }" manifestSource
  then
    throw "PinCollector PostgreSQL must initialise into a subdirectory of its volume mount"
  else
    pkgs.runCommand "pin-collector-postgres-data-contract-ok" { } "touch $out"
