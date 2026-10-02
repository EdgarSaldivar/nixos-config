{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 37066459424 from reviewed merge commit
  # 420707f0d4aa5ede9a5a45bb9b7b793a7e895584 (PRs 52-54: the matching-flow fixes and
  # transparent catalog cutouts). One new migration, 20261002_0034, is additive: a
  # nullable catalog_media.display_image_uri with a partial index, two nullable audit
  # columns on catalog_admin_actions, and the clear_display_image action type in its
  # CHECK constraint. No backfill. Both OCI revision labels and the API baked build
  # fingerprint (read from the published image layer) were verified before rollout.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  gitRevision = "420707f0d4aa5ede9a5a45bb9b7b793a7e895584";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:c93ffb4857b5adcefd4aea9c87acfcaf4e524212e7e26fdc0102e17d22b9058d";
  apiImageRevision = "420707f0d4aa5ede9a5a45bb9b7b793a7e895584";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:05edff83ad9edb26bd5f204630e13a14af7c834672747b8bb8e061ef2aa44ad8";
  modelImageRevision = "420707f0d4aa5ede9a5a45bb9b7b793a7e895584";
}
