{
  # Dungeon Scriber Phase 1: tailnet-only, no public route. See
  # docs/runbooks/minas-tirith/dungeon-scriber.md for what each gate does and the order
  # in which they are raised and lowered. With every gate false the frozen manifest
  # declares no Pod and no Secret: workloads are at zero replicas, the migration Job is
  # suspended, and pelargir reads no Dungeon Scriber SOPS keys. The runbook lists the
  # exact set of objects and host rules that merging this file still creates.
  #
  # The release is pinned and its secrets exist (secrets/dungeon-scriber.yaml), but it is
  # Enabled and served over tailnet HTTPS (tailscale serve on minas).
  #
  # Image published from reviewed Dungeon Scriber commit
  # c18317bdd4cf2ef7232340842eeba13192b50961. Its OCI index carries one linux/amd64
  # manifest, and that image's org.opencontainers.image.revision label was read from
  # the published config blob and matches the commit (User node, WorkingDir /app,
  # Cmd node apps/api/dist/server.js). The package is private, so pulls need the
  # dungeon-scriber-registry Secret.
  staged = true;
  enabled = true;
  # Set only after secrets/dungeon-scriber.yaml exists with every key the runbook
  # lists. Until then pelargir's sops-nix has nothing to decrypt and must not try.
  runtimeSecretReady = true;
  registryPullSecretReady = true;
  # Turns the api-tailnet Service into a NodePort. minas' raw-table gate for this port
  # is installed unconditionally, so it is already in place before this is raised.
  tailnetExposure = true;
  gitRevision = "c18317bdd4cf2ef7232340842eeba13192b50961";
  apiImage = "ghcr.io/edgarsaldivar/dungeon-scriber-api@sha256:fdceb7dcdc4d55d0d69a87e7500385fc5609e0ed2bb5107f78bd8f33c9ba8f50";
  apiImageRevision = "c18317bdd4cf2ef7232340842eeba13192b50961";

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
    # A static NodePort, chosen in the low band of 30000-32767 that Kubernetes prefers
    # to leave for explicit assignment. That makes a collision unlikely, not
    # impossible: random allocation falls back to this band when the upper one is
    # full, and another Service can request the same port explicitly. The runbook
    # checks the port is free before tailnetExposure is raised.
    port = 30080;
    interface = "tailscale0";
    # Tailnet client addresses. The direct NodePort preserves them
    # (externalTrafficPolicy: Local), and the API NetworkPolicy admits them only while
    # the plain-HTTP path is the intended one.
    clientCidr = "100.64.0.0/10";
    # Option 2: HTTPS through `tailscale serve` on minas (minas-tirith/dungeon-scriber.nix).
    # One value drives both hosts: minas starts Serve and closes the direct NodePort,
    # and pelargir's api-ingress admits no direct tailnet client. Trusting Serve's hop
    # (api.trustProxyHops = 1) is a separate, later commit. The runbook gives the
    # order in each direction.
    https = true;
  };
  api = {
    # 0 while clients connect directly over the tailnet; 1 behind Serve (or, later,
    # Traefik). 1 needs tailnet.https. Never change this and tailnet.https in the same
    # commit: see the runbook's Serve procedure.
    trustProxyHops = 1;
    logLevel = "info";
    defaultEntitlements = "beta-all";
  };
}
