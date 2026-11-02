{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images for reviewed master 8291bdefd3d0f5bd03dd9fed5b0e10540bea72a6 (PinCollector
  # PRs 218, 222, 223: the fixed on-device embedder in the app (app-only; models release
  # pinpipeline-models-v7), scan records for every scan with a replay tool and the legal
  # pages' wording for them, and search by name on a scan (POST /recognitions/name-search,
  # rate-limited per account). Alembic head becomes 20261101_scanrec01, through one migration:
  # - 20261101_scanrec01 adds scan_records (one row per decided scan per account: the
  #   on-device query vector, the pins shown, the decision and the scan's conditions; no
  #   read route; deleted with the account). Its downgrade drops the table.
  # An image-only rollback to 2885afe does not start: the API init container's
  # `alembic current --check-heads` refuses a database ahead of the image's head. A
  # rollback first needs the downgrade to 20261101_import01, which deletes every scan
  # record. No new feature gates. Stardust, Plus, notificationSender and collectorPages stay
  # as they were. Trade matching and account mail stay off.
  # Published by hand, not by the publish-images workflow (GitHub Actions minutes are spent):
  # buildx on nardol, linux/amd64, with provenance and SBOM. The API image was built from
  # the archived tree of 8291bde; its Python dependencies resolved to the same versions as
  # the live 2885afe image. The model image is the live 2885afe model image (the same
  # layers as 0cb2077's, 0e6543a's, 5e40a32's, b237cd9's and 4439ccc's) with only its
  # revision label set to 8291bde: model-service/, seed-data/ and .dockerignore are
  # byte-identical between 2885afe and 8291bde. Both OCI revision labels, the model image's
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
  gitRevision = "8291bdefd3d0f5bd03dd9fed5b0e10540bea72a6";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:21d73717169dfbeb0729dc70e43e3d541929a9e8494563dcf8bf45fd3396bec7";
  apiImageRevision = "8291bdefd3d0f5bd03dd9fed5b0e10540bea72a6";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:825259b5e50626eb43765a25ec1a7a6938ea7086cf89f4aa84134370f9c5c4c6";
  modelImageRevision = "8291bdefd3d0f5bd03dd9fed5b0e10540bea72a6";
}
