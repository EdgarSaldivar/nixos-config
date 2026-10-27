{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images for reviewed master b237cd9023a162de136521e4e86ecd78e1558b25 (PinCollector
  # PRs 140-174: the Starlight look, Wave 1; stardust, Help the catalog, Plus with the
  # founding mark, the shop and Your sky, sold-for, Wave 2; the read-only admin stardust
  # box; follow-ups). Migrations 20261025_0052 to 20261026_0056; Alembic head becomes
  # 20261026_0056. Stardust and Plus ship off (PIN_COLLECTOR_STARDUST_ENABLED and
  # PIN_COLLECTOR_PLUS_ENABLED unset, default false): their routes answer 404 and the app
  # hides them. Live without a switch: sold-for, the catalog's original price and capture
  # kind, admin series sizes, the app-wide 422 handler, and the Starlight look on the public
  # web pages. Trade matching stays off. notificationSender and collectorPages stay on.
  # Account mail stays off, as before.
  # Rolling the image back to 4439ccc without a schema downgrade is safe only until the
  # first original price is recorded (4439ccc's CatalogMetadata rejects the new price keys),
  # and the 0053 downgrade refuses once one exists.
  # Published by hand, not by the publish-images workflow (GitHub Actions minutes are spent):
  # buildx on nardol, linux/amd64, with provenance and SBOM. The API image was built from
  # the archived tree of b237cd9. The model image is the live 4439ccc model image (same
  # layers) with only its revision label set to b237cd9: model-service/, seed-data/ and the
  # model Dockerfile are byte-identical between 4439ccc and b237cd9, and the requirements
  # are ranges, so a rebuild could have pulled untested torch/transformers versions.
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
  # stardust and plus stay off (absent) until the conversion has run.
  # The stardust conversion, deploy 3 of 4: --refresh right before switch-on, with the
  # owner's go (2026-10-27). It pays only history added since the apply.
  stardustConversion = "refresh";
  gitRevision = "b237cd9023a162de136521e4e86ecd78e1558b25";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:94c436357a164a7374d2065f1090c1f1a5ca89fcda70775895cd370a38fddeb6";
  apiImageRevision = "b237cd9023a162de136521e4e86ecd78e1558b25";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:aeb7714d24b946625e6259e172a752e0e8b57dad2ca3f00ab4dac0932c8c4b6f";
  modelImageRevision = "b237cd9023a162de136521e4e86ecd78e1558b25";
}
