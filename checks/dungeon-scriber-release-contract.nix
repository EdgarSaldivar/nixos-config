{
  lib,
  pkgs,
  nixosConfigurations,
  darwinConfigurations,
  ...
}:

# The Dungeon Scriber release is deployed by digest, from a reviewed revision, and
# only in gate order. This proves the contract rejects every way a placeholder or a
# half-finished release could be staged.
let
  contract = import ../hosts/nixos/minas-tirith/dungeon-scriber-release-contract.nix {
    inherit lib;
  };
  base = import ../hosts/nixos/minas-tirith/dungeon-scriber-release.nix;
  shaA = lib.concatStrings (lib.replicate 40 "a");
  shaB = lib.concatStrings (lib.replicate 40 "b");
  digest = lib.concatStrings (lib.replicate 64 "1");
  valid = base // {
    staged = true;
    enabled = true;
    runtimeSecretReady = true;
    registryPullSecretReady = true;
    tailnetExposure = true;
    gitRevision = shaA;
    apiImageRevision = shaA;
    apiImage = "ghcr.io/edgarsaldivar/dungeon-scriber-api@sha256:${digest}";
  };
  accepts = release: (builtins.tryEval (contract.assertValid release)).success;
in
if
  !accepts valid
  || !accepts base
  || accepts (base // { staged = true; })
  || accepts (valid // { apiImage = null; })
  || accepts (valid // { apiImage = "ghcr.io/edgarsaldivar/dungeon-scriber-api:latest"; })
  || accepts (valid // { apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:${digest}"; })
  || accepts (
    valid // { apiImage = "ghcr.io/edgarsaldivar/dungeon-scriber-api@sha256:${lib.concatStrings (lib.replicate 64 "0")}"; }
  )
  || accepts (valid // { gitRevision = builtins.substring 0 39 shaA; })
  || accepts (valid // { apiImageRevision = shaB; })
  || accepts (builtins.removeAttrs valid [ "apiImageRevision" ])
  || accepts (valid // { runtimeSecretReady = false; registryPullSecretReady = false; })
  || accepts (base // { registryPullSecretReady = true; })
  || accepts (base // { enabled = true; })
  || accepts (base // { tailnetExposure = true; })
  || accepts (valid // { tailnet = valid.tailnet // { port = 3001; }; })
  || accepts (valid // { storage = valid.storage // { postgresStorageClass = "local-path"; }; })
  || accepts (valid // { storage = valid.storage // { blobHostPath = "/var/lib/rancher/k3s/storage/blobs"; }; })
then
  throw "Dungeon Scriber release contract accepted a placeholder, mutable, mismatched or out-of-order release"
else
  pkgs.runCommand "dungeon-scriber-release-contract-ok" { } "touch $out"
