{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 38019445767 from reviewed merge commit
  # 876f4254c4cb063014e0aaf40ff33e0203e9513b (PR 88: catalog credit and suggest a
  # correction). One new additive migration, Alembic head 20261009_0045: nullable
  # pins.created_by_user_id (backfilled from completed catalog addition audits, then pin
  # submissions), nullable collection_items.add_source with a CHECK, user_profiles
  # show_name_on_credit and credit_hidden_by_admin_at, new tables catalog_pin_match_credits
  # (backfilled from kept scan evidence and duplicate-check picks), catalog_edit_suggestions
  # and catalog_correction_pauses, and the catalog_admin_actions type CHECK replaced to allow
  # the three correction actions (downgrade guarded).
  # Both OCI revision labels and the API baked build fingerprint (read from the published
  # image) were verified before rollout.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  gitRevision = "876f4254c4cb063014e0aaf40ff33e0203e9513b";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:3ba5e1cfa7536909a2adc018b176a71cced18384ee1ca5644b7d83974a90c68c";
  apiImageRevision = "876f4254c4cb063014e0aaf40ff33e0203e9513b";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:c35e99dabde21bb0098145aa3a9fc6881c6e8e665fa2144c67c01c5a3a041334";
  modelImageRevision = "876f4254c4cb063014e0aaf40ff33e0203e9513b";
}
