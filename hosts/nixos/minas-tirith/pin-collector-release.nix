{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images for reviewed master 0e6543a3a75b2b8972416edabd13d1a093b34a90 (PinCollector
  # PRs 183-200: Add-as-new upload retries and HEIC, forgiving duplicate matching, edited
  # pins keep the phone's fingerprint, keyboard fixes, 8-bit models v6 and stable model
  # copies, app diagnostics reports, and the "One entry per pin" backend: series and waves
  # as catalog entries, chasers, the owner's tidy-up and series tools, and where a copy
  # was got). Alembic head becomes 20261030_0061, through five migrations:
  # - 20261029_0058 cleans catalog text in place (invisible and doubled characters) and
  #   re-keys stardust series subjects to the cleaned keys. Its downgrade is a no-op: the
  #   cleaned characters aren't kept, and older code reads cleaned text as any text.
  # - 20261030_diag01 adds app_diagnostics_reports (30-day retention, pruned hourly by the
  #   API; app_diagnostics_prune_interval_seconds defaults to 3600). Downgrade drops it.
  # - 20261029_0059, _0060 and 20261030_0061 add series, waves, aliases and revision and
  #   suggestion set links, pins' set role and Released at, and collection_items'
  #   acquired_channel, acquired_park_id and acquired_shop_id. Additive, nothing
  #   backfilled. Each downgrade refuses once its tables or columns hold data.
  # An image-only rollback to 5e40a32 does not start: the API init container's
  # `alembic current --check-heads` refuses a database ahead of the image's head. A
  # rollback first needs the downgrades to 20261028_0057, which refuse after the owner's
  # tidy-up or once any collector says where a copy was got (that data step comes first).
  # No new feature gates: the new routes are on with the release. Stardust, Plus,
  # notificationSender and collectorPages stay as they were. Trade matching and account
  # mail stay off.
  # Published by hand, not by the publish-images workflow (GitHub Actions minutes are spent):
  # buildx on nardol, linux/amd64, with provenance and SBOM. The API image was built from
  # the archived tree of 0e6543a. The model image is the live 5e40a32 model image (the same
  # layers as b237cd9's and 4439ccc's) with only its revision label set to 0e6543a:
  # model-service/, seed-data/ and .dockerignore are byte-identical between 5e40a32 and
  # 0e6543a, and the requirements are ranges, so a rebuild could have pulled untested
  # torch/transformers versions. Both OCI revision labels, the model image's identical
  # layers, and the API's baked build fingerprint (read from the published image) were
  # verified before rollout.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  # Feature gates (docs/runbooks/minas-tirith/pin-collector.md, "Enabling a release").
  notificationSender = true;
  collectorPages = true;
  # Deploy 4 of 4: stardust and Plus on for everyone during the beta (the owner's go,
  # 2026-10-27), after the refresh Job completed and its report was read. Plus is free to
  # join while plus_beta_open stays at its default (true). The conversion Job is removed.
  stardust = true;
  plus = true;
  gitRevision = "0e6543a3a75b2b8972416edabd13d1a093b34a90";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:84b87e45be0d47892cf1097e7fbf084063578493f18b7f423631d95465d02f40";
  apiImageRevision = "0e6543a3a75b2b8972416edabd13d1a093b34a90";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:80a425426107b79261e146d6334b9ffd52e410f1ec297c3fe5b1e6a73c3e8236";
  modelImageRevision = "0e6543a3a75b2b8972416edabd13d1a093b34a90";
}
