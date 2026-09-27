# Render the Dungeon Scriber manifest from an already-validated release
# (minas-tirith/dungeon-scriber-release-contract.nix). A function of the release, so
# checks/dungeon-scriber-deployment-contract.nix renders the staged and exposed shapes
# through this same code instead of a copy that could drift from it.
#
# Keep the manifest permanently owned after its first activation. k3s does not prune a
# removed auto-deploy file, so `staged = false` renders an inert object set rather
# than dropping the file and leaving the previous release live.
{ lib, pkgs }:
release:
let
  zeroDigest = lib.concatStrings (lib.replicate 64 "0");
  apiImage =
    if release.staged then release.apiImage else "registry.invalid/dungeon-scriber/inert@sha256:${zeroDigest}";
  apiDigest = lib.last (lib.splitString "@sha256:" apiImage);
in
pkgs.replaceVars ../minas-tirith/manifests/dungeon-scriber.yaml.in {
  inherit apiImage;
  gitRevision = if release.staged then release.gitRevision else lib.concatStrings (lib.replicate 40 "0");
  inherit (release.placement) nodeName;
  inherit (release.storage)
    postgresStorageClass
    postgresSize
    blobHostPath
    blobCapacity
    ;
  trustProxyHops = toString release.api.trustProxyHops;
  inherit (release.api) logLevel defaultEntitlements;
  statefulReplicas = if release.staged then "1" else "0";
  apiReplicas = if release.enabled then "1" else "0";
  migrationSuspended = if release.enabled then "false" else "true";
  migrationJobName = "dungeon-scriber-migrate-${builtins.substring 0 12 apiDigest}";
  # The NodePort exists only while exposure is declared; minas drops it on every
  # interface but the tailnet regardless (minas-tirith/dungeon-scriber.nix).
  tailnetServiceSpec =
    if release.tailnetExposure then
      "{ type: NodePort, externalTrafficPolicy: Local, selector: { app: dungeon-scriber-api }, "
      + "ports: [{ name: http, port: 3001, targetPort: http, nodePort: ${toString release.tailnet.port} }] }"
    else
      "{ type: ClusterIP, selector: { app: dungeon-scriber-api }, ports: [{ name: http, port: 3001, targetPort: http }] }";
}
