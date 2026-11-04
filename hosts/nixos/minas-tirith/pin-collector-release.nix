{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images for reviewed master 1a3b6524b0295d98285aa22231c88aab6b049f73 (PinCollector
  # PRs 238-240). Server changes come from 239: a pin's picture is now its main photo's
  # transparent cutout when that photo has one (else the earliest cutout, as before);
  # /catalog/images serves every active front photo's own cutout on a visible pin (back,
  # disabled and hidden pins' cutouts stay private); the set grid, series picker and admin
  # photo scores show cutouts; and a maintenance command (app.maintenance.catalog_cutouts:
  # a read-only export, then a checked import) backfills cutouts made on a Mac with the
  # app's own pipeline. 238 and 240 are app/build only (the PinCollector scheme runs
  # Release). No migrations: Alembic head stays 20261102_photo01, so an image-only rollback
  # to 89cd890 starts. No new feature gates. Stardust, Plus, notificationSender and
  # collectorPages stay as they were. Trade matching and account mail stay off.
  # Published by hand, not by the publish-images workflow (GitHub Actions minutes are spent):
  # buildx on nardol, linux/amd64, with provenance and SBOM. The API image was built from
  # the archived tree of 1a3b652; its Python dependencies match the live 89cd890 image
  # except SQLAlchemy 2.1.6 -> 2.1.7 (a patch release). The model image is the live 89cd890
  # model image (the rebuilt, dinov3_embedder-only one) with only its revision label set to
  # 1a3b652: model-service/, seed-data/ and .dockerignore are byte-identical between
  # 89cd890 and 1a3b652. Both OCI revision labels, the model image's identical layers, and
  # the API's baked build fingerprint (read from the built image) were verified before
  # rollout. The model-service env vars PIN_COLLECTOR_PIN_PROCESSOR_REQUIRE_GPU and
  # PIN_COLLECTOR_PIN_BATCH_PROCESSOR_FAKE_GPU (manifests/pin-collector.yaml.in) are still
  # unused; a follow-up removes them.
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
  gitRevision = "1a3b6524b0295d98285aa22231c88aab6b049f73";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:cfdbdc4c681a3750e3c8080babaec0cc0836a0cd669e50e0297eda8bccac26e9";
  apiImageRevision = "1a3b6524b0295d98285aa22231c88aab6b049f73";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:4f05bc4da8e1d3cf821ca67bf2ba7a518c96399adb068abd9abe898d4cd108d9";
  modelImageRevision = "1a3b6524b0295d98285aa22231c88aab6b049f73";
}
