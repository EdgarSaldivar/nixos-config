{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 34399848191 from reviewed commit
  # 9e992f201feed8120627ff63f4392fb2170cdea6, merged by PR 28 (4baaca56).
  # Both OCI revision labels and the API baked build fingerprint were verified.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  gitRevision = "9e992f201feed8120627ff63f4392fb2170cdea6";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:10c7265aabf97b2a09eebe9b117cedfb908219512e108e034a5e66ae19257774";
  apiImageRevision = "9e992f201feed8120627ff63f4392fb2170cdea6";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:64019033f7783ac34d0b2b1dfc90027ccb3d10888de08d1d2994ec3de91b243f";
  modelImageRevision = "9e992f201feed8120627ff63f4392fb2170cdea6";
}
