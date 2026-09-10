{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 34418675972 from reviewed commit
  # 4dbd3325465995c64a7198829a998747b6d53166, merged by PR 29 (d8ede87b).
  # Both OCI revision labels and the API baked build fingerprint were verified.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  gitRevision = "4dbd3325465995c64a7198829a998747b6d53166";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:13bf7013c3f481808b34eed9e95e157fdaed5c7c59fc9ab35abde9fd3d99b605";
  apiImageRevision = "4dbd3325465995c64a7198829a998747b6d53166";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:dbee900e71462830c59d453737b770a9227119b9573fc406b3182a8a8b445361";
  modelImageRevision = "4dbd3325465995c64a7198829a998747b6d53166";
}
