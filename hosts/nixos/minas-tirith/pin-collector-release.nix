{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 36265720058 from reviewed merge commit
  # a86f0cab9b83deba0ccdddf114ce68cf56fb095d (PR 32: on-device pin cutout v2 and
  # the training feedback exporter). No new migrations since the previous release.
  # Both OCI revision labels and the API baked build fingerprint (read from the
  # published image layer) were verified before rollout.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  gitRevision = "a86f0cab9b83deba0ccdddf114ce68cf56fb095d";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:94bf1bf1aadbe31994e5cdb09ff7e9fc7af627a0c4e0e947257a1aaa4085778e";
  apiImageRevision = "a86f0cab9b83deba0ccdddf114ce68cf56fb095d";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:01b587a3f4594116734f4315258f6883571c9f6e9522e9c5792a9d56b0e24a36";
  modelImageRevision = "a86f0cab9b83deba0ccdddf114ce68cf56fb095d";
}
