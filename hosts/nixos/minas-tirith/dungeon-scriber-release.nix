{
  # Dungeon Scriber: tailnet HTTPS through Serve, and the public origin through minas'
  # Traefik once `public.enable` is set. See
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
  # de9dcc66ff100f80dcb07702df612f8816018427: 02fe8e3d with one server change, a license
  # registry entry for a second build of the phone's suggestion model, in the newer bundle
  # format and under its own model id (ADR 0009 amendment 2026-11-12). With its files in
  # the model dataset, the API serves it beside the current bundle, which app builds
  # before this one keep downloading unchanged. The files are placed by hand before the
  # switch (runbook, "Model dataset on minas"): they are content-addressed, so only the
  # new ones are added, and manifest.json is replaced last. An image whose registry lacks
  # an entry skips it, so the previous image serves the older models exactly as before
  # whether or not the new files are there. The API refuses to start on a manifest that
  # lists missing files, so the upload is checked complete before this is switched. No
  # migration, setting, Secret or volume is added, so the ConfigMap is unchanged and the
  # migration Job (renamed with the digest) finds nothing to apply. GitHub Actions was
  # unavailable, so the image was built from a clean `git archive` of that commit with
  # `docker buildx` (linux/amd64, min provenance) on the owner's build host and pushed to
  # the same package. Its OCI index carries one linux/amd64 manifest plus a build
  # attestation, the same shape as the previous release, and its
  # org.opencontainers.image.revision label was read from the published config and matches
  # the commit (User node, WorkingDir /app, Cmd node apps/api/dist/server.js). The package
  # is private, so pulls need the dungeon-scriber-registry Secret. The previous image
  # (02fe8e3d, sha256:94e2a707…) runs against this schema unchanged and is a way back; it
  # does not serve the new bundle. Below it, the 0055, 0054 and 0053 releases' rollback
  # steps still apply. Do not go further back than 3c0bf65d to a96d244b once any offline
  # hold exists: a96d244b does not know offline holds and serves what was said in them. No
  # production worker runs, and nardol's worker pin is unchanged.
  staged = true;
  enabled = true;
  # Set only after secrets/dungeon-scriber.yaml exists with every key the runbook
  # lists. Until then pelargir's sops-nix has nothing to decrypt and must not try.
  runtimeSecretReady = true;
  registryPullSecretReady = true;
  # Turns the api-tailnet Service into a NodePort. minas' raw-table gate for this port
  # is installed unconditionally, so it is already in place before this is raised.
  tailnetExposure = true;
  gitRevision = "de9dcc66ff100f80dcb07702df612f8816018427";
  apiImage = "ghcr.io/edgarsaldivar/dungeon-scriber-api@sha256:cffaf03844c85ecf4490a79ebf75a202c15eb62d7ff5d5bc5789c359fc7d26e7";
  apiImageRevision = "de9dcc66ff100f80dcb07702df612f8816018427";

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
    # The phone model files the API hosts (MODEL_ARTIFACT_DIR; manifest.json plus
    # files/<sha256>). A second hand-created dataset on the same pool, also a static
    # `local` PersistentVolume, and mounted read-only: the API only reads it, and the
    # owner places files by hand. The API refuses a directory whose files do not hash to
    # the registry's pins, and its require-model-dataset init container refuses a path
    # without the hand-written `.dungeon-scriber-model-root` sentinel, so an unmounted
    # dataset never serves an empty or stale manifest. The files are reproducible from
    # the owner's staging copy, so the dataset is not backed up (the backup never reads
    # /storage).
    modelDataset = "storage/dungeon-scriber/models";
    modelHostPath = "/storage/dungeon-scriber/models";
    modelCapacity = "10Gi";
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
  # ADR 0010 §4: the public origin, through minas' Traefik. Turning it on publishes the
  # route (minas, traefik-routes/catalog.nix) and admits the Traefik Pods to the API
  # on 3001 (pelargir, api-ingress). The DNS record is changed by hand in Cloudflare
  # and is not in this repository. Traefik is one proxy hop, like Serve.
  public = {
    enable = true;
    hostname = "dungeon.saldivar.io";
    # The Traefik Pods that api-ingress admits: namespace and `app` label of
    # manifests/traefik.yaml.
    ingressNamespace = "traefik";
    ingressApp = "traefik";
  };
  api = {
    # 0 while clients connect directly over the tailnet; 1 behind Serve or Traefik,
    # both of which are exactly one hop. 1 needs Serve or the public route, and is
    # refused while direct tailnet clients are admitted. Never change this and
    # tailnet.https in the same commit: see the runbook's Serve procedure.
    trustProxyHops = 1;
    logLevel = "info";
    defaultEntitlements = "beta-all";
    # Per-address auth limits (per 15 minutes). Public clients arrive through the
    # upstream edge host's TCP passthrough, so the API sees one address for all of them
    # and these act as a shared ceiling rather than a per-client one. They are raised
    # above the API's defaults (5 and 30) so one noisy client cannot lock out the table;
    # the per-account login throttle and the password-hash concurrency cap still apply.
    # Restore per-client limits once real client addresses reach Traefik (ROADMAP).
    loginRateLimitMax = 60;
    rotationRateLimitMax = 600;
    # SERVER_SPEAKER_NAMING_ENABLED: whether processing workers may read a campaign's
    # allowed enrolled voices to name diarized speakers. false is the off switch: the
    # API lists no voices to workers, refuses voice sample downloads and ignores names
    # a worker submits. Recordings and transcripts are unaffected either way.
    serverSpeakerNaming = true;
    # Whether the API serves the phone models in storage.modelHostPath. true mounts the
    # model volume read-only, adds the require-model-dataset init container and sets
    # MODEL_ARTIFACT_DIR. Raise it only after the dataset, its sentinel and the files
    # exist on minas (runbook). false is the off switch: the API serves an empty
    # model manifest and nothing else changes.
    modelArtifacts = true;
  };
}
