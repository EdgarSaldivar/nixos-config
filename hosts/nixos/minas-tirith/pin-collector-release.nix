{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 37547210604 from reviewed merge commit
  # b397862c991f5e03371fc8692458b23e511d792d (PRs 56-64: pin backs, offline mode, the
  # board-split v5 contract, 48 MP capture, calibrated DINOv3-B thresholds, the server-side
  # matching adapter, and scan-data originals with the training opt-out). One new migration,
  # 20261006_0035, is additive: user_profiles.share_scans_for_training (non-null boolean,
  # server default true, so no backfill) and a nullable scan_evidence_captures
  # .original_attached_at. Its downgrade refuses while any collector has opted out. Both OCI
  # revision labels and the API baked build fingerprint (read from the published image
  # layer) were verified before rollout.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  gitRevision = "b397862c991f5e03371fc8692458b23e511d792d";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:6de1bd566115386debb38c954169e248c16dce7636fd8e2c08c7f33922dce041";
  apiImageRevision = "b397862c991f5e03371fc8692458b23e511d792d";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:5619702cfb01fceb51d9c8fc379855960b049783879a85622c4361833ccd97a7";
  modelImageRevision = "b397862c991f5e03371fc8692458b23e511d792d";
}
