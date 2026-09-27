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
  shaA = lib.concatStrings (lib.replicate 40 "a");
  shaB = lib.concatStrings (lib.replicate 40 "b");
  digest = lib.concatStrings (lib.replicate 64 "1");
  # Synthetic fixtures, independent of whichever gates the deployed release raises.
  off = {
    staged = false;
    enabled = false;
    runtimeSecretReady = false;
    registryPullSecretReady = false;
    tailnetExposure = false;
    gitRevision = null;
    apiImage = null;
    apiImageRevision = null;
    placement.nodeName = "fixture-node";
    storage = {
      postgresStorageClass = "local-path-retain";
      postgresSize = "1Gi";
      blobDataset = "storage/fixture/blobs";
      blobHostPath = "/storage/fixture/blobs";
      blobCapacity = "1Gi";
    };
    tailnet = {
      port = 30080;
      interface = "tailscale0";
      clientCidr = "100.64.0.0/10";
      https = false;
    };
    api = {
      trustProxyHops = 0;
      logLevel = "info";
      defaultEntitlements = "beta-all";
    };
  };
  valid = off // {
    staged = true;
    enabled = true;
    runtimeSecretReady = true;
    registryPullSecretReady = true;
    tailnetExposure = true;
    gitRevision = shaA;
    apiImageRevision = shaA;
    apiImage = "ghcr.io/edgarsaldivar/dungeon-scriber-api@sha256:${digest}";
  };
  served = valid // {
    tailnet = valid.tailnet // { https = true; };
    api = valid.api // { trustProxyHops = 1; };
  };
  accepts = release: (builtins.tryEval (contract.assertValid release)).success;
in
if
  !accepts valid
  || !accepts off
  || !accepts served
  # The deployed release must itself be valid, whatever its gates.
  || !accepts (import ../hosts/nixos/minas-tirith/dungeon-scriber-release.nix)
  || accepts (off // { staged = true; })
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
  || accepts (off // { registryPullSecretReady = true; })
  || accepts (off // { enabled = true; })
  || accepts (off // { tailnetExposure = true; })
  || accepts (valid // { enabled = false; })
  || accepts (valid // { staged = false; enabled = false; })
  || accepts (off // { tailnet = off.tailnet // { https = true; }; })
  # Serve with 0 hops is the transitional state; more than one hop never is.
  || !accepts (served // { api = served.api // { trustProxyHops = 0; }; })
  || accepts (served // { api = served.api // { trustProxyHops = 2; }; })
  || accepts (valid // { api = valid.api // { trustProxyHops = 1; }; })
  || accepts (valid // { tailnet = valid.tailnet // { port = 30301; }; })
  || accepts (valid // { tailnet = valid.tailnet // { port = 3001; }; })
  || accepts (valid // { storage = valid.storage // { postgresStorageClass = "local-path"; }; })
  || accepts (valid // { storage = valid.storage // { blobHostPath = "/var/lib/rancher/k3s/storage/blobs"; }; })
then
  throw "Dungeon Scriber release contract accepted a placeholder, mutable, mismatched or out-of-order release"
else
  pkgs.runCommand "dungeon-scriber-release-contract-ok" { } "touch $out"
