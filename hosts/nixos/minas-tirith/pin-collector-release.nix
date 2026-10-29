{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images for reviewed master 5e40a3213637f25a7289a660c43c54a420e59f19 (PinCollector
  # PRs 175-182: the single-pin cutout on the Neural Engine, pin sightings with the Parks
  # map, faster scan uploads, scan recording gaps). Migration 20261028_0057 (the
  # pin_sightings table plus user_profiles.share_sightings, default true, and
  # sightings_prompted_at); Alembic head becomes 20261028_0057. The migration is additive,
  # so b237cd9's code works with the new schema, but an image-only rollback to b237cd9
  # does not start: the API init container's `alembic current --check-heads` refuses a
  # database ahead of the image's head. A rollback first needs the 0057 downgrade, which
  # drops every sighting and every collector's sightings choice.
  # Sightings need no switch: the defaults hold (a 15-minute delay, the API's own hourly
  # 30-day prune, 300 per account per day, the bundled OpenStreetMap shop list baked into
  # the image at /app/shared/places/places.json). The sightings reads are session-free and
  # have their own per-address budget. Stardust, Plus, notificationSender and collectorPages
  # stay as they were. Trade matching and account mail stay off.
  # Published by hand, not by the publish-images workflow (GitHub Actions minutes are spent):
  # buildx on nardol, linux/amd64, with provenance and SBOM. The API image was built from
  # the archived tree of 5e40a32. The model image is the live b237cd9 model image (the same
  # layers as 4439ccc's) with only its revision label set to 5e40a32: model-service/,
  # seed-data/ and .dockerignore are byte-identical between b237cd9 and 5e40a32, and the
  # requirements are ranges, so a rebuild could have pulled untested torch/transformers
  # versions. Both OCI revision labels, the model image's identical layers, and the API's
  # baked build fingerprint (read from the published image) were verified before rollout.
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
  gitRevision = "5e40a3213637f25a7289a660c43c54a420e59f19";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:e1a22278a52ec6d0594112b9f61733a986ab22a36376d3daa8e063b438153fb5";
  apiImageRevision = "5e40a3213637f25a7289a660c43c54a420e59f19";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:a1670b5331e7791a206b9e8dee006a7649a2192629c4dfb679763508c6aa2ea5";
  modelImageRevision = "5e40a3213637f25a7289a660c43c54a420e59f19";
}
