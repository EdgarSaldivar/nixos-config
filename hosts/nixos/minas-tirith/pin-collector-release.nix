{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 40148501244 from reviewed merge commit
  # 4439cccfa0e38e80313cb9168cffa8427085215e (PRs 134 collector page in the app; 136-138
  # trade matches wave 5 = matching, offers, in-person trades, word filter, trade moderation;
  # 139 send-notifications retries its first database connection). Migration 20261024_0051
  # (trade matches); Alembic head becomes 20261024_0051. Trade matching ships off
  # (PIN_COLLECTOR_TRADE_MATCHING_ENABLED unset, default false): every trading route answers
  # 404 and the app shows today's Trades page. notificationSender and collectorPages stay on.
  # Account mail stays off (no Resend key mounted yet), so password reset answers 503
  # mail_unavailable.
  # Both OCI revision labels and the API baked build fingerprint (read from the published
  # image) were verified before rollout.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  # Feature gates (docs/runbooks/minas-tirith/pin-collector.md, "Enabling a release").
  notificationSender = true;
  collectorPages = true;
  gitRevision = "4439cccfa0e38e80313cb9168cffa8427085215e";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:d0ff2b3926e5dad0615f3f0aa1eae01c98a3115faa1fda34a215a46d2ede89b5";
  apiImageRevision = "4439cccfa0e38e80313cb9168cffa8427085215e";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:0d082e57edfab59d96c8a5f70b4ea1d6f808c64b296df2e1186351e291f3bbb9";
  modelImageRevision = "4439cccfa0e38e80313cb9168cffa8427085215e";
}
