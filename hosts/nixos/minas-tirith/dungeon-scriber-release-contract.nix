{ lib }:
let
  isFullGitRevision = value: builtins.isString value && builtins.match "[0-9a-f]{40}" value != null;
  zeroDigest = lib.concatStrings (lib.replicate 64 "0");

  # An all-zero digest is the inert renderer's own placeholder; accepting it here
  # would let a copied example pass as a release.
  isPinnedApiImage =
    value:
    builtins.isString value
    && builtins.match "ghcr\\.io/edgarsaldivar/dungeon-scriber-api@sha256:[0-9a-f]{64}" value != null
    && !lib.hasSuffix zeroDigest value;

  revisionContractSatisfied =
    release:
    let
      gitRevision = release.gitRevision or null;
      apiImageRevision = release.apiImageRevision or null;
    in
    (gitRevision == null && apiImageRevision == null)
    || (
      isFullGitRevision gitRevision && isFullGitRevision apiImageRevision && apiImageRevision == gitRevision
    );

  isNodePort = value: builtins.isInt value && value >= 30000 && value <= 32767;
in
{
  inherit isFullGitRevision isPinnedApiImage revisionContractSatisfied;

  assertValid =
    release:
    assert lib.assertMsg (
      !release.staged || isFullGitRevision (release.gitRevision or null)
    ) "A staged Dungeon Scriber release must declare its reviewed full lowercase Git revision";
    assert lib.assertMsg (revisionContractSatisfied release)
      "The Dungeon Scriber API OCI revision must be a full Git SHA matching the reviewed revision";
    assert lib.assertMsg (
      !release.staged || isPinnedApiImage (release.apiImage or null)
    ) "A staged Dungeon Scriber release must use an immutable, non-placeholder GHCR sha256 reference";
    assert lib.assertMsg (
      !release.staged || (release.runtimeSecretReady && release.registryPullSecretReady)
    ) "Dungeon Scriber cannot be staged before its SOPS runtime and GHCR pull Secrets exist";
    assert lib.assertMsg (
      !release.registryPullSecretReady || release.runtimeSecretReady
    ) "The Dungeon Scriber registry Secret is applied after the runtime Secret's namespace wait; raise runtimeSecretReady first";
    assert lib.assertMsg (
      !release.enabled || release.staged
    ) "Dungeon Scriber cannot be enabled before it is staged";
    assert lib.assertMsg (
      !release.tailnetExposure || release.enabled
    ) "Dungeon Scriber cannot expose an API that is not enabled";
    assert lib.assertMsg (
      builtins.isString release.placement.nodeName && release.placement.nodeName != ""
    ) "Dungeon Scriber placement must name a node";
    assert lib.assertMsg (isNodePort release.tailnet.port) "The Dungeon Scriber tailnet port is a NodePort and must be 30000-32767";
    assert lib.assertMsg (
      release.storage.postgresStorageClass == "local-path-retain"
    ) "Dungeon Scriber PostgreSQL must use the Retain storage class";
    assert lib.assertMsg (
      lib.hasPrefix "/storage/" release.storage.blobHostPath
    ) "The Dungeon Scriber blob store must live on a ZFS dataset under /storage";
    release;
}
