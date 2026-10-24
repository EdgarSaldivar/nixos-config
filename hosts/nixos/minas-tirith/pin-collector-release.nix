{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 40132761913 from reviewed merge commit
  # 41e85857b7900859d2579ae72d38e14863488586 (PRs 126-128 coming back wave 3 = push
  # notifications, widgets; 129 and 135 privacy page; 130 iOS route test; 131 account
  # mail via Resend; 132-133 sharing wave 4 = collector pages, public pin pages, universal
  # links). Migrations 20261019_0049 (notifications) and 20261023_0050 (collector pages);
  # Alembic head becomes 20261023_0050. notificationSender starts the send-notifications
  # CronJob (no APNs key yet: it sends nothing, still expires and prunes events);
  # collectorPages mounts the collector-link key and sets "Get the app" to /support until
  # a TestFlight invite exists. Account mail stays off (no Resend key mounted yet), so
  # password reset answers 503 mail_unavailable.
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
  gitRevision = "41e85857b7900859d2579ae72d38e14863488586";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:c5115825ca4a3549cd9e0cd73ef508cf8647f435faf47924aa7ae18e8310f87d";
  apiImageRevision = "41e85857b7900859d2579ae72d38e14863488586";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:95dbefce5f320dfaa242d9105f7758b5f84f902044fbbb966460fe58db688de8";
  modelImageRevision = "41e85857b7900859d2579ae72d38e14863488586";
}
