{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images for reviewed master 2edc480ca6f30dbe723742b1d306b850feb76f50 (PinCollector
  # PRs 224 and 230-235). Server changes come from two of them:
  # - 230 warns when part of the catalog has no fingerprint in the space a scan searched
  #   (an info warning on recognition responses; counts cached per process);
  # - 231 adds spreadsheet-import follow-ups (a catalog lookup cache, limits and caps, and
  #   CSV and To scan fixes).
  # The rest is app or tool only: scan records for every scan and for "Do I have this?"
  # checks (224, 232), board "Not pins" with Select and swipes (233), the refit For sale
  # check (235), and a board hard-negatives training tool (234). No migrations: Alembic
  # head stays 20261101_scanrec01, so an image-only rollback to 8291bde starts. No new
  # feature gates. Stardust, Plus, notificationSender and collectorPages stay as they
  # were. Trade matching and account mail stay off.
  # Published by hand, not by the publish-images workflow (GitHub Actions minutes are spent):
  # buildx on nardol, linux/amd64, with provenance and SBOM. The API image was built from
  # the archived tree of 2edc480; its Python dependencies match the live 8291bde image
  # except psycopg and psycopg-binary 3.3.6 -> 3.3.7 (a patch release). The model image is
  # the live 8291bde model image (the same layers as 2885afe's, 0cb2077's, 0e6543a's,
  # 5e40a32's, b237cd9's and 4439ccc's) with only its revision label set to 2edc480:
  # model-service/, seed-data/ and .dockerignore are byte-identical between 8291bde and
  # 2edc480. Both OCI revision labels, the model image's identical layers, and the API's
  # baked build fingerprint (read from the built image) were verified before rollout.
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
  gitRevision = "2edc480ca6f30dbe723742b1d306b850feb76f50";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:4d302b1c8f7f35a4e7f070eaa930e6feb239e6dc0a81a5895b6d18e2e6cc5a4a";
  apiImageRevision = "2edc480ca6f30dbe723742b1d306b850feb76f50";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:41b44404b31189ad7e2eca872340a11d62360218eb45440f4247282623e878fe";
  modelImageRevision = "2edc480ca6f30dbe723742b1d306b850feb76f50";
}
