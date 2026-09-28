{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 36380271684 from reviewed merge commit
  # 94e8ef7068e66db8ae711637a72eeb14ecd9579e (PR 34: SigV4 presigning, which Garage
  # requires). No new migrations since the previous release. Both OCI revision labels and
  # the API baked build fingerprint (read from the published image layer) were verified
  # before rollout.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  gitRevision = "94e8ef7068e66db8ae711637a72eeb14ecd9579e";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:6fea204672768082ce75b1cca9c1c21d4a82ffcade63b3859771706c51b9e5ad";
  apiImageRevision = "94e8ef7068e66db8ae711637a72eeb14ecd9579e";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:05df178a15ae39a5dec36a83b0cdbe90520f5e4b04bb01fe390f43a7f136e84b";
  modelImageRevision = "94e8ef7068e66db8ae711637a72eeb14ecd9579e";
}
