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
  # b4298e8c536110f3c4145fba31ecd8d80726adc3: d7922ecb plus #148 to #191. Server changes:
  # homebrew capabilities and delivery (an app is served only the homebrew, characters and
  # cards it can read, and is told to update otherwise), Codex factions and structured facts
  # (migration 0056: a faction kind and a codex_facts table with hold triggers), and feature
  # text of the SRD character pack as table rows (catalog 63e03d1f, the same mechanics as
  # 95f1ff7e, so stored characters stay accepted and are re-pinned on their next write).
  # CODEX_ENGINE_FEATURES and HOMEBREW_ENGINE_FEATURES stay unset (empty), so factions, facts
  # and new homebrew engine features are refused until the owner lists them; the ConfigMap is
  # unchanged. The license registry adds teacher-only entries; the API serves the same five
  # phone models. The migration Job (renamed with the digest) applies 0056.
  # GitHub Actions was unavailable, so the image was built from a clean `git archive` of
  # that commit with `docker buildx` (linux/amd64, min provenance) on the owner's build
  # host and pushed to the same package. Its OCI index carries one linux/amd64 manifest
  # plus a build attestation, the same shape as the previous release, and its
  # org.opencontainers.image.revision label matches the commit (User node, WorkingDir /app,
  # Cmd node apps/api/dist/server.js). The package is private, so pulls need the
  # dungeon-scriber-registry Secret, and the runbook proves a pull of this digest with it
  # on the API's node before the switch. The previous image (d7922ecb, sha256:0826dffd…)
  # runs on this schema and is a way back after one SQL step that pins characters on
  # 63e03d1f back to 95f1ff7e (the release runbook's rollback-repin.sql); without it, d7922ecb
  # answers 409 rule_pack_unavailable for characters written under this image. The earlier
  # releases' rollback notes (ab54d537, de9dcc66, 0053 to 0055, and never below a96d244b once
  # an offline hold exists) still apply below that. No production worker runs, and nardol's
  # worker pin is unchanged.
  staged = true;
  enabled = true;
  # Set only after secrets/dungeon-scriber.yaml exists with every key the runbook
  # lists. Until then pelargir's sops-nix has nothing to decrypt and must not try.
  runtimeSecretReady = true;
  registryPullSecretReady = true;
  # Turns the api-tailnet Service into a NodePort. minas' raw-table gate for this port
  # is installed unconditionally, so it is already in place before this is raised.
  tailnetExposure = true;
  gitRevision = "b4298e8c536110f3c4145fba31ecd8d80726adc3";
  apiImage = "ghcr.io/edgarsaldivar/dungeon-scriber-api@sha256:842415e76d11c8e044685215ceae9257037c745912221c0065217f2622c949e3";
  apiImageRevision = "b4298e8c536110f3c4145fba31ecd8d80726adc3";

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
