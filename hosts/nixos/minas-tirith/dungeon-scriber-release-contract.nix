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
      isFullGitRevision gitRevision
      && isFullGitRevision apiImageRevision
      && apiImageRevision == gitRevision
    );

  # The static-allocation band of the default 30000-32767 range: min(max(16, 2768/32), 128)
  # = 86 ports. Random NodePort allocation prefers the upper band, which makes a
  # collision here unlikely but not impossible: it falls back to this band when the upper
  # one is full, and another explicit assignment can pick the same port. The runbook
  # checks the port is free before raising exposure.
  isStaticNodePort = value: builtins.isInt value && value >= 30000 && value < 30086;
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
    assert lib.assertMsg (!release.registryPullSecretReady || release.runtimeSecretReady)
      "The Dungeon Scriber registry Secret is applied after the runtime Secret's namespace wait; raise runtimeSecretReady first";
    assert lib.assertMsg (
      !release.enabled || release.staged
    ) "Dungeon Scriber cannot be enabled before it is staged";
    assert lib.assertMsg (
      !release.tailnetExposure || release.enabled
    ) "Dungeon Scriber cannot expose an API that is not enabled";
    assert lib.assertMsg (
      builtins.isString release.placement.nodeName && release.placement.nodeName != ""
    ) "Dungeon Scriber placement must name a node";
    assert lib.assertMsg (
      !release.tailnet.https || release.tailnetExposure
    ) "Tailnet HTTPS proxies to the NodePort; it needs tailnetExposure";
    assert lib.assertMsg (
      !release.public.enable || release.enabled
    ) "Dungeon Scriber cannot publish an API that is not enabled";
    assert lib.assertMsg (
      builtins.isString release.public.hostname && lib.hasSuffix ".saldivar.io" release.public.hostname
    ) "The Dungeon Scriber public hostname must be a saldivar.io name (the wildcard certificate)";
    # Every path that may reach the API is either direct (no proxy) or exactly one
    # proxy hop (Serve on minas, or Traefik). Trusting a hop is safe only while no
    # direct client can reach the API, because a direct client could otherwise forge
    # the forwarded header the API would believe:
    #   * direct tailnet clients admitted (exposure without Serve): 0 hops;
    #   * otherwise, behind Serve and/or the public route: 0 or 1. Serve or Traefik
    #     with 0 hops is the transitional state the API rolls through before a
    #     direct path opens (and after it closes); the runbook gives the order;
    #   * with neither proxy in front: 0.
    assert lib.assertMsg
      (
        let
          directTailnet = release.tailnetExposure && !release.tailnet.https;
          proxied = (release.tailnetExposure && release.tailnet.https) || release.public.enable;
        in
        if directTailnet then
          release.api.trustProxyHops == 0
        else if proxied then
          lib.elem release.api.trustProxyHops [
            0
            1
          ]
        else
          release.api.trustProxyHops == 0
      )
      "The API trusts no hop while direct tailnet clients are admitted, and at most one hop behind Serve or Traefik";
    assert lib.assertMsg (isStaticNodePort release.tailnet.port)
      "The Dungeon Scriber tailnet port must be a static-band NodePort (30000-30085)";
    assert lib.assertMsg (
      release.storage.postgresStorageClass == "local-path-retain"
    ) "Dungeon Scriber PostgreSQL must use the Retain storage class";
    assert lib.assertMsg (lib.hasPrefix "/storage/" release.storage.blobHostPath)
      "The Dungeon Scriber blob store must live on a ZFS dataset under /storage";
    release;
}
