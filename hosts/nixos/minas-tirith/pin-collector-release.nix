{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 37814467189 from reviewed merge commit
  # 4b8e1af8ceb1577827a606fe6c2ef5b1595ab9d8 (PRs 72-75: board-scan diagnostics with killed-scan
  # reports, deleting an indexed pin's vectors before its images, waiting evidence sent at
  # app launch, a test-only route warm-up). One new migration, 20261007_0038
  # (board_scan_exits), applied by the migrate Job. Both OCI revision labels and the API baked
  # build fingerprint (read from the published image) were verified before rollout.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  gitRevision = "4b8e1af8ceb1577827a606fe6c2ef5b1595ab9d8";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:7481324fe7e001a6dc02a7e3c60b815469742de5fb751f26536dc4e6087cbffd";
  apiImageRevision = "4b8e1af8ceb1577827a606fe6c2ef5b1595ab9d8";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:2de4f9d8e56b4c403692d811ba17d0cf0af433465e4113d27d5b1e5068dc2d17";
  modelImageRevision = "4b8e1af8ceb1577827a606fe6c2ef5b1595ab9d8";
}
