{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 37898578956 from reviewed merge commit
  # 977ea54f87158d9270aa8bb4740f6371104aac53 (PR 80: admin catalog edits with revision
  # history; PR 79: the trade journal; PR 81: trade cards, with pin editions on collection
  # items). Two new additive migrations: 20261009_0040 creates catalog_pin_revisions, and
  # Alembic head 20261009_0041 creates trade_journal_entries and trade_journal_items; no
  # existing table changes and nothing is backfilled. Both OCI revision labels and the API
  # baked build fingerprint (read from the published image) were verified before rollout.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  gitRevision = "977ea54f87158d9270aa8bb4740f6371104aac53";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:dec53c7a386be0b7e9eaea0ca3a61985c89565ca1386bbc893e10ceaedc8b527";
  apiImageRevision = "977ea54f87158d9270aa8bb4740f6371104aac53";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:5e985883c5f4e8aeb1fc521ded15c01bdd94753203654ca44cc8717f425ca70e";
  modelImageRevision = "977ea54f87158d9270aa8bb4740f6371104aac53";
}
