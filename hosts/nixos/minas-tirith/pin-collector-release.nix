{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 36454303945 from reviewed merge commit
  # ba91fd25f3405a6ad67719804a16321122a0be85 (PR 33: the training pull as a hand-started
  # Kubernetes Job, on top of 94e8ef7's SigV4 presigning). No new migrations since the
  # previous release. Both OCI revision labels and the API baked build fingerprint (read
  # from the published image layer) were verified before rollout.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  gitRevision = "ba91fd25f3405a6ad67719804a16321122a0be85";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:b371d91cfddf02c1d20493d7d599ebb2224325877a62034db8a8cdc2080bb53a";
  apiImageRevision = "ba91fd25f3405a6ad67719804a16321122a0be85";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:d2d3b8d5d73c552da5486b1a4df1bdba27b6c83a2628976ba61c807187143490";
  modelImageRevision = "ba91fd25f3405a6ad67719804a16321122a0be85";
}
