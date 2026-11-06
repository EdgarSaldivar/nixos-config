{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Release 1859b2b: PRs 243 and 244 (Suggest details, Step 1: suggestions from catalog neighbours only, off by
  # default behind a database switch at /admin/ui/suggestions, global cap 8 a day; no outside service, no new secret),
  # 245 (a test fix), 246 (an empty series card in Search) and 247 (the admin queue shows collector reports and likely
  # duplicates only; no data change). Three migrations, all additive: 20261105_suggest01 (four new tables),
  # 20261105_suggest02 (character becomes correctable: the field check is widened, plus one new table) and
  # 20261105_suggest03 (one new table). The head moves from 20261102_scancat01 to 20261105_suggest03. An image-only
  # rollback to 7752c13 needs no database change: the old code never reads the new tables, and the widened check still
  # accepts every old value. No new feature gates. Stardust, Plus, notificationSender and collectorPages stay as they
  # were. Trade matching and account mail stay off. Published by hand, not by the publish-images workflow (GitHub
  # Actions minutes are spent): buildx on nardol, linux/amd64. The API image was built from the archived tree of
  # 1859b2b, with provenance and SBOM. The model image is the live 7752c13 model image with only its revision label
  # set to 1859b2b, without a provenance attestation: model-service/, seed-data/ and .dockerignore are byte-identical
  # between 7752c13 and 1859b2b, and its layers match the live image's. Both OCI revision labels and the API's baked
  # build fingerprint (read from the built image) were verified before rollout.
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
  # Suggest details' outside lookups (PinCollector Step 3): mounts SerpApi's and the reader's
  # keys into the API. Set it in the release that first pins an image with Step 3, after
  # both keys are in secrets/pin-collector.yaml (docs/runbooks/minas-tirith/pin-collector.md).
  # The keys alone turn nothing on: the admin chooses SerpApi and Qwen at /admin/ui/suggestions.
  suggestDetailsKeys = false;
  gitRevision = "1859b2b3f7412baa6c983d7af9f85a626cdc6e57";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:41b846d9c9edd8fee501b73670776441a1af590bfb18e690be706aef4893df4b";
  apiImageRevision = "1859b2b3f7412baa6c983d7af9f85a626cdc6e57";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:9e17f3d20c29dddbc62578718e89f74161f3e2d05a33db3f1118e40ec367189f";
  modelImageRevision = "1859b2b3f7412baa6c983d7af9f85a626cdc6e57";
}
