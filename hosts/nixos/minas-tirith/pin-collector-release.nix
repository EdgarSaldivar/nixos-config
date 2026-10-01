{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 36819011033 from reviewed merge commit
  # 093cdb69074da52ccc861e439976b67b180d2ba3 (PR 38: the Storybook redesign, with its
  # stacked PRs 39-43). One new migration, 20260929_0033, adds the collection items'
  # for_trade and is_grail flags as non-null booleans with a server default of false, so it
  # needs no backfill. Both OCI revision labels and the API baked build fingerprint (read
  # from the published image layer) were verified before rollout.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  gitRevision = "093cdb69074da52ccc861e439976b67b180d2ba3";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:748bda30f3f1e40f5fdc1ebfd7c28bcc8c0f699532dd35ebb6c95017f79c4c2e";
  apiImageRevision = "093cdb69074da52ccc861e439976b67b180d2ba3";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:eae914ba3a32decda438baed4c8f3777678ada4406e9698924f785a1eeabfaf8";
  modelImageRevision = "093cdb69074da52ccc861e439976b67b180d2ba3";
}
