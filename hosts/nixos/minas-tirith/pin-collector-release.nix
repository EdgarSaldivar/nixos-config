{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 38585956443 from reviewed merge commit
  # ded6c90bb24a79ada29c2ec9eae8671146433b3e (PRs 99-121: admin pages on Jinja, lab digest
  # module, version groups and a confirm step before catalog merges, own-item report/block
  # hidden, the app polish pass, plus additive response fields `listed_by_name` on blocks
  # and `warnings` on /recognitions/embedding). No new migration: Alembic head stays
  # 20261009_0046.
  # Both OCI revision labels and the API baked build fingerprint (read from the published
  # image) were verified before rollout.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  gitRevision = "ded6c90bb24a79ada29c2ec9eae8671146433b3e";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:4776df8d747681592aa3fcff2a9da69e437e9bdc7d9d81941986ea20a250bf6b";
  apiImageRevision = "ded6c90bb24a79ada29c2ec9eae8671146433b3e";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:37e4047393e61212e9ae438b81bcd70bc1467cdb6b805c18135bb279be360043";
  modelImageRevision = "ded6c90bb24a79ada29c2ec9eae8671146433b3e";
}
