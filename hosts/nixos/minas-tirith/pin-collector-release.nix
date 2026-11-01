{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images for reviewed master 0cb2077575afe6853a26f16d4e2c518960fff7b5 (PinCollector
  # PRs 201-207: the catalog series app form and pin page and "where you got it" in the app
  # (app-only), a faster board scan (mask logits read without NSNumber, per-square timings in
  # scan_metrics), a board's pins matched with the server four at a time, and full scan
  # diagnostics: each scan's steps in the app diagnostics report, a compute plan for every
  # model, and "Send diagnostics now"). Alembic head becomes 20261031_diag02, through one
  # migration:
  # - 20261031_diag02 widens ck_app_diagnostics_reports_trigger to take 'manual' (a report
  #   the collector sent from Settings). The per-scan rows travel in the report JSONB. Its
  #   downgrade deletes the manual reports first, then narrows the check back.
  # An image-only rollback to 0e6543a does not start: the API init container's
  # `alembic current --check-heads` refuses a database ahead of the image's head. A
  # rollback first needs the downgrade to 20261030_0061, which deletes the manual reports.
  # No new feature gates. Stardust, Plus, notificationSender and collectorPages stay as they
  # were. Trade matching and account mail stay off.
  # Published by hand, not by the publish-images workflow (GitHub Actions minutes are spent):
  # buildx on nardol, linux/amd64, with provenance and SBOM. The API image was built from
  # the archived tree of 0cb2077. The model image is the live 0e6543a model image (the same
  # layers as 5e40a32's, b237cd9's and 4439ccc's) with only its revision label set to
  # 0cb2077: model-service/, seed-data/ and .dockerignore are byte-identical between 0e6543a
  # and 0cb2077, and the requirements are ranges, so a rebuild could have pulled untested
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
  gitRevision = "0cb2077575afe6853a26f16d4e2c518960fff7b5";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:ee447fecfbe279d99614f3877368d11dc69cdffa7fe956ee77f42619eebfcd78";
  apiImageRevision = "0cb2077575afe6853a26f16d4e2c518960fff7b5";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:d95effa527a453dc0c32d32d671395223a7ac5e1754d88b6874397af7b1cb0c6";
  modelImageRevision = "0cb2077575afe6853a26f16d4e2c518960fff7b5";
}
