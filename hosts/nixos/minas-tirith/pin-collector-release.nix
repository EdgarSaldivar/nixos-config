{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images for reviewed master 2885afed3d217fce4e00bac3e0b6b0731cdf1bd6 (PinCollector
  # PRs 208-221: a saved board keeps each pin's cutout; Pin Match Lab and the app's SAM2
  # removed (code only; no table is dropped); a free Apple ID builds from a pull; edit
  # several pins at once (batch change, remove and restore with request ids); two lookalikes
  # side by side and "How to tell them apart" notes (off for every embedder registered before
  # dinov3b-norm-v2); a model crash guard that retries after a background kill (app-only);
  # the fixed on-device embedder's contract (dinov3b-norm-v2) and a catalog re-embed tool
  # (inert until the re-embed is applied and apps send v2); bring in a spreadsheet and the
  # To scan shelf). Alembic head becomes 20261101_import01, through three migrations:
  # - 20261101_0062 adds collection_item_requests (batch request ids; additive).
  # - 20261101_twin01 adds lookalike_notes and widens the stardust rule/reason and the
  #   moderation subject checks; its downgrade is guarded.
  # - 20261101_import01 adds the import and To scan tables and widens
  #   ck_collection_items_add_source to take 'import'; its downgrade refuses while any
  #   import exists. An older image can't read an item whose add_source is 'import'.
  # An image-only rollback to 0cb2077 does not start: the API init container's
  # `alembic current --check-heads` refuses a database ahead of the image's head. A
  # rollback first needs the downgrades to 20261031_diag02 (import01 refuses while imports
  # exist). No new feature gates. Stardust, Plus, notificationSender and collectorPages stay
  # as they were. Trade matching and account mail stay off.
  # Published by hand, not by the publish-images workflow (GitHub Actions minutes are spent):
  # buildx on nardol, linux/amd64, with provenance and SBOM. The API image was built from
  # the archived tree of 2885afe; its Python dependencies resolved to the same versions as
  # the live 0cb2077 image. The model image is the live 0cb2077 model image (the same
  # layers as 0e6543a's, 5e40a32's, b237cd9's and 4439ccc's) with only its revision label
  # set to 2885afe: model-service/, seed-data/ and .dockerignore are byte-identical between
  # 0cb2077 and 2885afe, and the requirements are ranges, so a rebuild could have pulled
  # untested torch/transformers versions. Both OCI revision labels, the model image's
  # identical layers, and the API's baked build fingerprint (read from the built image)
  # were verified before rollout.
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
  gitRevision = "2885afed3d217fce4e00bac3e0b6b0731cdf1bd6";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:46896d5e3c8537da92c26560eb565b050c018661db3e28d26b033d8b25ee971a";
  apiImageRevision = "2885afed3d217fce4e00bac3e0b6b0731cdf1bd6";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:ab67d92307126a98e44f57a0b2d05ad563e111652116ea21d994ad265f289413";
  modelImageRevision = "2885afed3d217fce4e00bac3e0b6b0731cdf1bd6";
}
