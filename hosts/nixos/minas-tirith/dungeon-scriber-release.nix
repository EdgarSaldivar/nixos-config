{
  # Dungeon Scriber Phase 1: tailnet-only, no public route. See
  # docs/runbooks/minas-tirith/dungeon-scriber.md for what each gate does and the order
  # in which they are raised and lowered. With every gate false the frozen manifest
  # declares no Pod and no Secret: workloads are at zero replicas, the migration Job is
  # suspended, and pelargir reads no Dungeon Scriber SOPS keys. The runbook lists the
  # exact set of objects and host rules that merging this file still creates.
  #
  # The release is pinned but NOT staged: staging needs secrets/dungeon-scriber.yaml
  # (runtimeSecretReady and registryPullSecretReady), which does not exist yet.
  #
  # Image published from reviewed Dungeon Scriber commit
  # 1586d72417a0dbd088f42677cc7b92512451a00c. Its OCI index carries one linux/amd64
  # manifest, and that image's org.opencontainers.image.revision label was read from
  # the published config blob and matches the commit (User node, WorkingDir /app,
  # Cmd node apps/api/dist/server.js). The package is private, so pulls need the
  # dungeon-scriber-registry Secret.
  staged = false;
  enabled = false;
  # Set only after secrets/dungeon-scriber.yaml exists with every key the runbook
  # lists. Until then pelargir's sops-nix has nothing to decrypt and must not try.
  runtimeSecretReady = false;
  registryPullSecretReady = false;
  # Turns the api-tailnet Service into a NodePort. minas' raw-table gate for this port
  # is installed unconditionally, so it is already in place before this is raised.
  tailnetExposure = false;
  gitRevision = "1586d72417a0dbd088f42677cc7b92512451a00c";
  apiImage = "ghcr.io/edgarsaldivar/dungeon-scriber-api@sha256:6ecb0895aeac12c446237504c3b1843757d3db0e55bb56958e3f11c1560456e1";
  apiImageRevision = "1586d72417a0dbd088f42677cc7b92512451a00c";

  # ADR 0010 §6: placement, storage and endpoints are configuration, not literals
  # scattered through the manifest. The template, the minas firewall gate, the
  # backup program and the contract checks all read these values.
  placement = {
    nodeName = "minas-tirith";
  };
  storage = {
    postgresStorageClass = "local-path-retain";
    postgresSize = "20Gi";
    # A dedicated ZFS dataset on the `storage` pool, created by hand (disko may
    # never touch these pools) and backed up, age-encrypted, to storage2. ADR 0006
    # needs local POSIX rename/fsync, so this is a static `local` PersistentVolume,
    # never a network filesystem. local-path-retain would put it on the root NVMe,
    # which is ext4 rather than ZFS.
    # The volume is only a directory path to kubelet: an unmounted dataset leaves an
    # empty mountpoint directory that would mount fine. The API's require-blob-dataset
    # init container and the backup program both refuse a path that lacks the
    # hand-written `.dungeon-scriber-blob-root` sentinel, and the backup also requires
    # blobDataset to be mounted exactly at blobHostPath.
    blobDataset = "storage/dungeon-scriber/blobs";
    blobHostPath = "/storage/dungeon-scriber/blobs";
    blobCapacity = "500Gi";
  };
  tailnet = {
    # A static NodePort. It sits in the low band of 30000-32767 that Kubernetes keeps
    # for explicit assignment (the first 86 ports for this range size) and never hands
    # out at random, so no other Service can take it before exposure is raised.
    port = 30080;
    interface = "tailscale0";
    # Tailnet client addresses. The direct NodePort preserves them
    # (externalTrafficPolicy: Local), and the API NetworkPolicy admits them only while
    # the plain-HTTP path is the intended one.
    clientCidr = "100.64.0.0/10";
    # Option 2: HTTPS through `tailscale serve` on minas (minas-tirith/dungeon-scriber.nix).
    # One value drives both hosts: minas starts Serve and closes the direct NodePort,
    # and pelargir renders an API that trusts exactly one proxy hop and admits no
    # direct tailnet client. The runbook gives the rebuild order in each direction.
    https = false;
  };
  api = {
    # 0 while clients connect directly over the tailnet; 1 behind Serve (or, later,
    # Traefik). The contract ties it to tailnet.https.
    trustProxyHops = 0;
    logLevel = "info";
    defaultEntitlements = "beta-all";
  };
}
