{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Release 1994d1a: PR 241, the stardust feel and already-owned pins. No migration: the head stays at
  # 20261102_scancat01, so an image-only rollback to 9a23749 needs no database change. No new feature gates.
  # Stardust, Plus, notificationSender and collectorPages stay as they were. Trade matching and account mail stay
  # off. Published by hand, not by the publish-images workflow (GitHub Actions minutes are spent): buildx on nardol,
  # linux/amd64. The API image was built from the archived tree of 1994d1a, with provenance and SBOM. The model
  # image is the live 9a23749 model image's base with only its revision label set to 1994d1a, without a provenance
  # attestation: model-service/, seed-data/ and .dockerignore are byte-identical between 9a23749 and 1994d1a, and
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
  gitRevision = "1994d1a32a3c01fd91b2c1ba965e76b66d52b40d";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:6a605478fc4ee82e7ef5275e864cf4e59084e519ddf31e138509606d367f7010";
  apiImageRevision = "1994d1a32a3c01fd91b2c1ba965e76b66d52b40d";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:33c98b021417f6189d41fcb646fac178f501444ac8c01025ad041ad169d6ab63";
  modelImageRevision = "1994d1a32a3c01fd91b2c1ba965e76b66d52b40d";
}
