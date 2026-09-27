{
  # Dungeon Scriber Phase 1: tailnet-only, no public route. See
  # docs/runbooks/minas-tirith/dungeon-scriber.md for what each gate does and the order
  # in which they may be raised. Every gate below is false, so the frozen manifest
  # renders an inert object set (zero replicas, suspended migration Job) and pelargir
  # reads no Dungeon Scriber SOPS keys.
  #
  # The release is pinned but NOT staged: staging needs secrets/dungeon-scriber.yaml
  # (runtimeSecretReady and registryPullSecretReady), which does not exist yet.
  #
  # Image published from reviewed Dungeon Scriber commit
  # 8e047a9e0719b97ad4feb12ba979314aa8222fd9. Its OCI index carries one linux/amd64
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
  gitRevision = "8e047a9e0719b97ad4feb12ba979314aa8222fd9";
  apiImage = "ghcr.io/edgarsaldivar/dungeon-scriber-api@sha256:5d7d053096a0af1022c0564d27b7656944e2ab730ca5fde5be502cddec739a79";
  apiImageRevision = "8e047a9e0719b97ad4feb12ba979314aa8222fd9";

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
