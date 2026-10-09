{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 38002658260 from reviewed merge commit
  # 52043621238c6218fa5a7ea7d62dc9565bdd65c0 (PR 83: backend backlog cleanup; PR 84: iOS
  # backlog cleanup; PR 85: whole-wall check; PR 86: pin backs Phase 1, catalog version
  # groups; PR 87: backlog docs). Three new additive migrations: 20261009_0042 adds
  # nullable prior_created_at and prior_for_trade columns (with two CHECK constraints on
  # them) to trade_journal_items, 20261009_0043 replaces the match-outcome surface check
  # to also allow wall_check, and Alembic head 20261009_0044 creates catalog_pin_versions
  # and catalog_pin_version_events; nothing is backfilled.
  # Both OCI revision labels and the API baked build fingerprint (read from the published
  # image) were verified before rollout.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  gitRevision = "52043621238c6218fa5a7ea7d62dc9565bdd65c0";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:a2838e25beb4aeac5f18a4829a84c175f9ba5148899df4dd51dcf846a8aa9b8f";
  apiImageRevision = "52043621238c6218fa5a7ea7d62dc9565bdd65c0";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:07f1ad01cbd1e5ddae63dbcf774c516450ee91db182e7e526157057d68e6f566";
  modelImageRevision = "52043621238c6218fa5a7ea7d62dc9565bdd65c0";
}
