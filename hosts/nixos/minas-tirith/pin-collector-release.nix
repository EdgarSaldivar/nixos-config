{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Release 4794a66: PRs 248 (backlog notes), 249 (a scan record's fingerprint reads back as the float32 it was stored
  # as; a production fix), 250 (a hidden automatic duplicate candidate no longer blocks Remove Submission), 251 (Suggest
  # details Step 3, outside lookups through SerpApi and the owner's reader; built in but off: it needs both keys, which
  # suggestDetailsKeys keeps unmounted here, and then the admin's choice at /admin/ui/suggestions), 252 and 253 (app
  # changes; the server side of 253 only accepts the new diagnostics fields), 254 (backend dependencies installed from
  # constraints.txt, the live image's own versions) and 255 (test fixes). No migrations: the head stays
  # 20261105_suggest03, so an image-only rollback to 1859b2b needs no database change. No feature gate changes;
  # suggestDetailsKeys stays false. Published by hand, not by the publish-images workflow (GitHub Actions minutes are
  # spent): buildx on nardol, linux/amd64. The API image was built from the archived tree of 4794a66, with provenance
  # and SBOM. The model image is the live 1859b2b model image's base with only its revision label set to 4794a66,
  # without a provenance attestation: model-service/, seed-data/ and .dockerignore are byte-identical between 1859b2b
  # and 4794a66, and its RootFS matches the live image's. Both OCI revision labels and the API's baked build
  # fingerprint (read from the built image) were verified before rollout.
  #
  # Keys release (same images as 4794a66, owner's "lets do step 3", 2026-11-06): suggestDetailsKeys goes on, mounting
  # SerpApi's key and the reader gateway's own key from secrets/pin-collector.yaml into the API's runtime Secret. The
  # API restarts onto them; no image, migration or other gate changes. Mounting turns nothing on by itself: outside
  # lookups start only when an admin picks SerpApi and Qwen at /admin/ui/suggestions. Rollback: pick "Our catalog
  # only" there (no deploy), or set this back to false.
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
  suggestDetailsKeys = true;
  gitRevision = "4794a66fa8a1a48873fdb0dc2a59a9624e34c641";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:79a2b0a09f4de59ff0d03c5c1aef8b2a745781a38f206dec2670ffed677bfcdb";
  apiImageRevision = "4794a66fa8a1a48873fdb0dc2a59a9624e34c641";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:97824ad82780a163c141dd8919de67e5e13b35bef6484233d5dbedea2779aecd";
  modelImageRevision = "4794a66fa8a1a48873fdb0dc2a59a9624e34c641";
}
