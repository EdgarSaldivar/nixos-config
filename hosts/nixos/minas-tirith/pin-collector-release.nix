{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images for reviewed master 89cd890128414991350e221edab9ee50d41ddc19 (PinCollector
  # PRs 225-227, 229 and 236). A PRODUCTION MIGRATION that deletes data: Alembic head
  # becomes 20261102_photo01, through three migrations:
  # - 20261102_batchhash01 (229) keeps what each edit-several entry asked with its request id
  #   (collection_item_requests.payload_sha256), so a reused id with another change is
  #   refused. Its downgrade drops the column.
  # - 20261102_droplab01 (226) DROPS the 23 retired Pin Match Lab tables (golden_truth_*,
  #   crop_evaluation_*, crop_reviews, matching_eval_*) with every row in them. Nothing has
  #   read them since PR 210. The owner chose to purge them without the runbook's separate
  #   dump (2026-11-03: "they arent useful to us anymore"); the nightly restic snapshots
  #   still hold them for their retention. Its downgrade recreates the tables empty.
  # - 20261102_photo01 (227) adds catalog_media_scores, the automatic pick of each pin's
  #   catalog photo, and an index on scan_records.confirmed_pin_id. Its downgrade drops both.
  # Also: 236 lets a later scan-record decision on re-matched pins replace the earlier one,
  # and 225 removes the retired server-side pin processors (SAM2) from the API and the model
  # service. An image-only rollback to 2edc480 does not start: the API init container's
  # `alembic current --check-heads` refuses a database ahead of the image's head. A rollback
  # first needs the downgrade to 20261101_scanrec01 (the Lab tables come back empty). No new
  # feature gates. Stardust, Plus, notificationSender and collectorPages stay as they were.
  # Trade matching and account mail stay off.
  # Published by hand, not by the publish-images workflow (GitHub Actions minutes are spent):
  # buildx on nardol, linux/amd64, with provenance and SBOM. The API image was built from
  # the archived tree of 89cd890; its Python dependencies are identical to the live 2edc480
  # image. The model image is a real rebuild (model-service/ changed): it serves only
  # dinov3_embedder, pin_processor and pin_batch_processor are gone, and its requirements
  # are pinned to the live image's versions, which it installs. Without the old
  # PYTHONPATH=/app it keeps the Triton base image's own PYTHONPATH (DALI's wheel folder),
  # so five base-image packages (nvidia-dali and its deps) are visible to Python again;
  # numpy, torch and transformers resolve to the pinned versions. Both OCI revision labels
  # and the API's baked build fingerprint (read from the built image) were verified before
  # rollout. The model-service env vars PIN_COLLECTOR_PIN_PROCESSOR_REQUIRE_GPU and
  # PIN_COLLECTOR_PIN_BATCH_PROCESSOR_FAKE_GPU (manifests/pin-collector.yaml.in) are now
  # unused; a follow-up removes them.
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
  gitRevision = "89cd890128414991350e221edab9ee50d41ddc19";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:cf6f17dde66d34152d34c320a7f59d455212c616666d9b78fc347acec873373e";
  apiImageRevision = "89cd890128414991350e221edab9ee50d41ddc19";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:7527147fd0fc84fb5b2f78acac461f655abefa49483218efcd158047206abb76";
  modelImageRevision = "89cd890128414991350e221edab9ee50d41ddc19";
}
