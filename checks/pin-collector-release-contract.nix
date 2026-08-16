{ lib, pkgs, nixosConfigurations, darwinConfigurations, ... }:

# A digest alone identifies bytes, but not the reviewed source revision the
# image claims to contain. Require both published images to carry independently
# recorded full Git revisions that match the one approved for the release.
  let
    contract = import ../hosts/nixos/minas-tirith/pin-collector-release-contract.nix {
      inherit lib;
    };
    shaA = lib.concatStrings (lib.replicate 40 "a");
    shaB = lib.concatStrings (lib.replicate 40 "b");
    valid = {
      staged = true;
      gitRevision = shaA;
      apiImageRevision = shaA;
      modelImageRevision = shaA;
    };
    inactive = {
      staged = false;
      gitRevision = null;
      apiImageRevision = null;
      modelImageRevision = null;
    };
    accepts = release: (builtins.tryEval (contract.assertValid release)).success;
  in
  if
    !accepts valid
    || !accepts inactive
    || accepts (inactive // { gitRevision = "malformed"; })
    || accepts (valid // { gitRevision = builtins.substring 0 39 shaA; })
    || accepts (valid // { apiImageRevision = "A${builtins.substring 1 39 shaA}"; })
    || accepts (valid // { modelImageRevision = builtins.substring 0 39 shaA; })
    || accepts (builtins.removeAttrs valid [ "apiImageRevision" ])
    || accepts (valid // { apiImageRevision = shaB; })
    || accepts (valid // { modelImageRevision = shaB; })
  then
    throw "PinCollector release revision contract accepted malformed or mismatched evidence"
  else
    pkgs.runCommand "pin-collector-release-contract-ok" { } "touch $out"
