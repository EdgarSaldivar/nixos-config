{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 38041811441 from reviewed merge commit
  # 4c2b97fb1fac8d6c286b4b7f5534c2a1bcd4d4bf (PRs 89-98: duplicate-check thresholds per
  # contract, merged-credit chains, CI cache and production guard, iOS wall/evidence fixes,
  # public catalog without uploader ids, moderation v1 reports/blocks + admin queue + app,
  # ViewModels split). One new additive migration, Alembic head 20261009_0046: report
  # subject/reason/resolution columns and CHECKs on moderation_reports, per-subject block
  # columns and a unique key on user_blocks; both tables held 0 rows in production before
  # rollout (read-only check 2026-10-10).
  # Both OCI revision labels and the API baked build fingerprint (read from the published
  # image) were verified before rollout.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  gitRevision = "4c2b97fb1fac8d6c286b4b7f5534c2a1bcd4d4bf";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:2d202a9a2b32f6e4db882d18605233aa9e6a9e8529224c134cf0b449d8a314a3";
  apiImageRevision = "4c2b97fb1fac8d6c286b4b7f5534c2a1bcd4d4bf";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:9328036833a2721da14b6bbcde085ba73621c60292196d2c9a7a522000b03924";
  modelImageRevision = "4c2b97fb1fac8d6c286b4b7f5534c2a1bcd4d4bf";
}
