{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 37718823239 from reviewed merge commit
  # 655cec7315262899ff6506e706c447885838e734 (PR 72: board-scan diagnostics — scan metrics
  # with board evidence, reports of board scans killed for memory, an operator view). One
  # new migration, 20261007_0038 (board_scan_exits), applied by the migrate Job. Both OCI
  # revision labels and the API baked build fingerprint (read from the published image)
  # were verified before rollout.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  gitRevision = "655cec7315262899ff6506e706c447885838e734";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:d9ffb395509b6acdddf50613f317562718af8f03c5d5c947c017ffe231006a30";
  apiImageRevision = "655cec7315262899ff6506e706c447885838e734";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:880a3ae2272aa287dc42081832431a4257992741cbdb1fa677d677ed7ba4b7aa";
  modelImageRevision = "655cec7315262899ff6506e706c447885838e734";
}
