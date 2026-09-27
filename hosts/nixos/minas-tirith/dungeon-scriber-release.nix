{
  # Dungeon Scriber Phase 1: tailnet-only, no public route. See
  # docs/runbooks/minas-tirith/dungeon-scriber.md for what each gate does and the order
  # in which they may be raised. Every gate below is false, so the frozen manifest
  # renders an inert object set (zero replicas, suspended migration Job) and pelargir
  # reads no Dungeon Scriber SOPS keys.
  #
  # ⛔ PLACEHOLDERS, deliberately null rather than a fake digest. The release contract
  # refuses `staged = true` until all three are real: `apiImage` must be an immutable
  # ghcr.io/edgarsaldivar/dungeon-scriber-api@sha256:<64 hex> reference published by
  # the Dungeon Scriber repository's CI, and both revisions must be the reviewed
  # 40-character commit that the image's org.opencontainers.image.revision label
  # independently reports.
  staged = false;
  enabled = false;
  # Set only after secrets/dungeon-scriber.yaml exists with every key the runbook
  # lists. Until then pelargir's sops-nix has nothing to decrypt and must not try.
  runtimeSecretReady = false;
  registryPullSecretReady = false;
  # Turns the api-tailnet Service into a NodePort. minas' raw-table gate for this port
  # is installed unconditionally, so it is already in place before this is raised.
  tailnetExposure = false;
  gitRevision = null;
  apiImage = null;
  apiImageRevision = null;

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
    blobHostPath = "/storage/dungeon-scriber/blobs";
    blobCapacity = "500Gi";
  };
  tailnet = {
    # A NodePort, so it must sit in the cluster's 30000-32767 range. Clients use
    # http://<node MagicDNS name>:<port>.
    port = 30301;
    interface = "tailscale0";
  };
  api = {
    # 0 while clients connect directly over the tailnet; 1 once Traefik fronts it.
    trustProxyHops = 0;
    logLevel = "info";
    defaultEntitlements = "beta-all";
  };
}
