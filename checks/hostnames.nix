{
  lib,
  pkgs,
  nixosConfigurations,
  darwinConfigurations,
  ...
}:

# A flake output named `foo` whose networking.hostName is `bar` produces
# a machine you deploy by one name and that calls itself another. Caught
# exactly this: dol-amroth was configured as "dol-amorth" for months.
let
  mismatched = lib.filterAttrs (n: c: c.config.networking.hostName != n) (
    nixosConfigurations // darwinConfigurations
  );
  names = lib.mapAttrsToList (n: c: "${n} -> ${c.config.networking.hostName}") mismatched;
in
if mismatched == { } then
  pkgs.runCommand "hostnames-ok" { } "touch $out"
else
  throw "flake output name != networking.hostName for: ${lib.concatStringsSep ", " names}"
