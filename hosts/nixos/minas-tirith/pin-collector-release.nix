{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 37852562553 from reviewed merge commit
  # 1c9422b8313fe0fc76387e220cdb8d08a793f2f1 (PR 76: draft-submission rollback order after
  # finalize, the passport "Contributed" count from catalog additions, the old public-profile
  # route retired). No new migration; the Alembic head stays 20261007_0038. Both OCI revision
  # labels and the API baked build fingerprint (read from the published image) were verified
  # before rollout.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  gitRevision = "1c9422b8313fe0fc76387e220cdb8d08a793f2f1";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:c08679e6e9273043170aaf5655e16cc59205c546db66cb1fddc1548510bac4ec";
  apiImageRevision = "1c9422b8313fe0fc76387e220cdb8d08a793f2f1";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:e4d65cca3e8f07cb4ffe8798c655ec258cde24cee8892f4b99b5ad250abd5900";
  modelImageRevision = "1c9422b8313fe0fc76387e220cdb8d08a793f2f1";
}
