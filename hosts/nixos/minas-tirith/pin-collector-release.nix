{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Release d003c9b (the owner's "go for it, do all your recs", 2026-11-09): the admin redesign, PRs 265-270. A
  # sidebar with counts and a Today page, the release workbench and settings, the catalog table with its drawer and bulk
  # edits, the review queues as workbenches, Suggestions as switches, find from anywhere, a phone layout, and the last
  # f-string admin page retired (Pico removed). The admin UI plus the server code behind it (the two readings of a
  # disputed sheet field kept in the row's flags JSON, hiding a single release row, the catalog's batched reads). No
  # migrations and no schema change, so an image-only rollback to 6053fd1 is safe. The model image is the live 6053fd1 model image's base with only its
  # revision label set to d003c9b (model-service/, seed-data/ and .dockerignore unchanged; RootFS matches the live
  # image's). Published by hand on nardol like 6053fd1; both revision labels and the API's baked build fingerprint
  # were verified before rollout. No gate changes.
  #
  # Earlier, still in force:
  # Release 6053fd1: PRs 256 (the app's "Found on" field removed), 257, 259 and 261 (monthly release sheets:
  # migrations 20261109_release01 to release03, the Releases admin page, public GET /releases, sheets read by the
  # reader's vision model with Tesseract as cross-check and fallback, Disney's art on release rows behind its own
  # switch, first real photos of a release pin held for an admin), 258, 260, 262 and 263 (the app's Home tab,
  # releases, cards and Your 2026 in pins) and 264 (sheet panels padded for the vision reader). The three migrations
  # only add tables and columns. An older image's migration Job wouldn't know the new head, so a rollback is a fix
  # forward or an explicit downgrade, not an image swap. Published by hand, not by the publish-images workflow
  # (GitHub Actions minutes are spent): buildx on nardol, linux/amd64. The API image was built from the archived tree
  # of 6053fd1, with provenance and SBOM. The model image is the live 4794a66 model image's base with only its
  # revision label set to 6053fd1, without a provenance attestation: model-service/, seed-data/ and .dockerignore are
  # byte-identical between 4794a66 and 6053fd1, and its RootFS matches the live image's. Both OCI revision labels and
  # the API's baked build fingerprint (read from the built image) were verified before rollout.
  #
  # releaseImport goes on in this release (the owner's "yes turn it on, and run it", 2026-11-09): it unsuspends the
  # release-import CronJob. The job fetches from the feed only while the fetch switch at /admin/ui/releases is on;
  # release art stays off there. Nothing it reads is public until an admin approves it. Rollback: turn the fetch
  # switch off (no deploy), or set releaseImport back to false.
  #
  # Step 3 keys (since 2026-11-06, the owner's "lets do step 3"): suggestDetailsKeys mounts SerpApi's key and the
  # reader gateway's own key from secrets/pin-collector.yaml into the API's runtime Secret. Mounting turns nothing on
  # by itself: outside lookups start only when an admin picks SerpApi and Qwen at /admin/ui/suggestions. Rollback:
  # pick "Our catalog only" there (no deploy), or set this back to false.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  # Feature gates (docs/runbooks/minas-tirith/pin-collector.md, "Enabling a release").
  notificationSender = true;
  collectorPages = true;
  # Deploy 4 of 4: stardust and Plus on for everyone during the beta (the owner's go,
  # 2026-10-27), after the refresh Job completed and its report was read. Plus is free to
  # join while plus_beta_open stays at its default (true). The conversion Job is removed.
  stardust = true;
  plus = true;
  # Suggest details' outside lookups (PinCollector Step 3): mounts SerpApi's and the reader's
  # keys into the API. Set it in the release that first pins an image with Step 3, after
  # both keys are in secrets/pin-collector.yaml (docs/runbooks/minas-tirith/pin-collector.md).
  # The keys alone turn nothing on: the admin chooses SerpApi and Qwen at /admin/ui/suggestions.
  suggestDetailsKeys = true;
  # PinCollector's monthly release sheets: unsuspends the release-import CronJob. Needs suggestDetailsKeys (the
  # reader's key). The feed fetch has its own switch at /admin/ui/releases (docs/runbooks/minas-tirith/pin-collector.md).
  releaseImport = true;
  gitRevision = "d003c9b939f35c3ea1fde81aedf37cfbd747992e";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:3bec797cde382994068343ae8adbb652324c212bed7fe63c77b791992becd046";
  apiImageRevision = "d003c9b939f35c3ea1fde81aedf37cfbd747992e";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:5ebc61f48b8b7b5470253e747d6821f0f4e94821ca6fea49ed1ff1d25b64a83d";
  modelImageRevision = "d003c9b939f35c3ea1fde81aedf37cfbd747992e";
}
