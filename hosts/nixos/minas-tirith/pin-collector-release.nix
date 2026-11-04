{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Release 9a23749: PR 228, Add my scans to the catalog. Migration 20261102_scancat01 is additive: a
  # user_profiles.add_scans_to_catalog column, true for every account, and a catalog_scan_photo_tries table. The
  # head moves from 20261102_photo01, so an image-only rollback to 1a3b652 can't be assumed to start: its image
  # doesn't know the new head. No new feature gates: the setting is on by default, by the owner's go.
  # Stardust, Plus, notificationSender and collectorPages stay as they were. Trade matching and account mail stay
  # off. Published by hand, not by the publish-images workflow (GitHub Actions minutes are spent): buildx on nardol,
  # linux/amd64. The API image was built from the archived tree of 9a23749, with provenance and SBOM. The model
  # image is the live 1a3b652 model image's base with only its revision label set to 9a23749, without a provenance
  # attestation: model-service/, seed-data/ and .dockerignore are byte-identical between 1a3b652 and 9a23749, and
  # its layers match the live image's. Both OCI revision labels and the API's baked build fingerprint (read from the
  # built image) were verified before rollout.
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
  gitRevision = "9a237495fad7e36af8248c7c870e0b724429330a";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:c616187adb8d4bbc903c5ff21eb000dbfa4efd01d6e7310e415ec1926b3f22cf";
  apiImageRevision = "9a237495fad7e36af8248c7c870e0b724429330a";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:72ea8d7e2c20e65d532f0c959ab947218d20060600f5b43fa948bccedd5bae76";
  modelImageRevision = "9a237495fad7e36af8248c7c870e0b724429330a";
}
