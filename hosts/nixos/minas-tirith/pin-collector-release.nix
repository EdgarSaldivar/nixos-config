{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 37867978062 from reviewed merge commit
  # 426d5bb114759d516339a9bf3c74a4fd93b1f847 (PR 77: collection adds take a client_request_id
  # and a repeat returns the row it made; PR 78: iOS review follow-ups and a backlog audit).
  # New additive migration: Alembic head 20261008_0039 adds collection_items.client_request_id
  # and a partial unique index; nothing is backfilled. Both OCI revision labels and the API
  # baked build fingerprint (read from the published image) were verified before rollout.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  gitRevision = "426d5bb114759d516339a9bf3c74a4fd93b1f847";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:a0de0373f30629b67fb3daad02829c392b6b69cb03cd7744a7038da8e0b19f2b";
  apiImageRevision = "426d5bb114759d516339a9bf3c74a4fd93b1f847";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:2c485e531e2016832aad8890dcb68efd406579b9055a4b1ff46ed2eb24943a2c";
  modelImageRevision = "426d5bb114759d516339a9bf3c74a4fd93b1f847";
}
