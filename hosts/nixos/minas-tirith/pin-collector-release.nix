{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 37699089079 from reviewed merge commit
  # aed8810e81e4036beda0f6b122ac76ff7dbb8b83 (PR 68: the "Do I have this?" quick check —
  # check pack and matching-adapter endpoints, match outcomes — plus back photos kept out
  # of visual recognition). One new migration, 20261007_0037 (match_outcomes), applied by
  # the migrate Job. Both OCI revision labels and the API baked build fingerprint (read from
  # the published image layer) were verified before rollout.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  gitRevision = "aed8810e81e4036beda0f6b122ac76ff7dbb8b83";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:ae655889fbc763ea1d8bf7a842b94f30cf68d7c0227307a04c12cf464a6c1a36";
  apiImageRevision = "aed8810e81e4036beda0f6b122ac76ff7dbb8b83";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:c6d959d4502921e07ddecbc440516b73bc13ce4471541b55de4904efee8e2d37";
  modelImageRevision = "aed8810e81e4036beda0f6b122ac76ff7dbb8b83";
}
