{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Release 7752c13: PR 242, a front photo added by hand brings the phone's fingerprint and cutout. No migration: the
  # head stays at 20261102_scancat01, so an image-only rollback to 1994d1a needs no database change. No new feature
  # gates. Stardust, Plus, notificationSender and collectorPages stay as they were. Trade matching and account mail stay
  # off. Published by hand, not by the publish-images workflow (GitHub Actions minutes are spent): buildx on nardol,
  # linux/amd64. The API image was built from the archived tree of 7752c13, with provenance and SBOM. The model
  # image is the live 1994d1a model image's base with only its revision label set to 7752c13, without a provenance
  # attestation: model-service/, seed-data/ and .dockerignore are byte-identical between 1994d1a and 7752c13, and
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
  gitRevision = "7752c135356b0060dc187f69faae5f05e47f5872";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:b5ba09a43073b5e9c9d330fff14b564d4756335180cf941fdefcbe9c62921d4d";
  apiImageRevision = "7752c135356b0060dc187f69faae5f05e47f5872";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:4c883de80e626641b25f4e0803a053204e3f4f9be4acd6f073dff38aa0506820";
  modelImageRevision = "7752c135356b0060dc187f69faae5f05e47f5872";
}
