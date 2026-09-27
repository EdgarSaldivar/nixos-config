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
  # The complete API ConfigMap data. It is rendered into the ConfigMap and hashed into
  # the Pod template from this one value, so any edit to any key rolls the API.
  apiConfig = {
    NODE_ENV = "production";
    HOST = "0.0.0.0";
    PORT = "3001";
    BLOB_ROOT = "/var/lib/dungeon-scriber/blobs";
    TRUST_PROXY_HOPS = toString release.api.trustProxyHops;
    LOG_LEVEL = release.api.logLevel;
    DEFAULT_ENTITLEMENTS = release.api.defaultEntitlements;
  };
  # JSON is valid YAML, and builtins.toJSON quotes every value as a string.
  apiConfigData = builtins.toJSON apiConfig;
  configHash = builtins.hashString "sha256" apiConfigData;
  # Direct tailnet clients are admitted only while Serve is off, and the contract then
  # requires trustProxyHops = 0. Serve on with 0 hops is the transitional state that
  # lets the API roll to 0 hops BEFORE this policy opens (see the runbook).
  directTailnet = release.tailnetExposure && !release.tailnet.https;
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
  inherit apiConfigData configHash;
  apiIngressRules =
    if directTailnet then
      "[{ from: [{ ipBlock: { cidr: ${release.tailnet.clientCidr} } }], ports: [{ protocol: TCP, port: 3001 }] }]"
    else
      "[]";
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
