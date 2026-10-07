{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 37590939113 from reviewed merge commit
  # 4c70b98994f9f81e716e037e4e255c9aa8b3401a (PRs 66-67: the cellular-originals switch and
  # the Neural Engine board model, contract boarddet-birefnet-v6 with v5 still accepted).
  # No new migrations since the previous release. Both OCI revision labels and the API
  # baked build fingerprint (read from the published image layer) were verified before
  # rollout.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  gitRevision = "4c70b98994f9f81e716e037e4e255c9aa8b3401a";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:eec645d847a82c642a088b662972784d514d30fbc6ee72faa7c10e87515aa540";
  apiImageRevision = "4c70b98994f9f81e716e037e4e255c9aa8b3401a";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:2140ef068fecbcea7fcb78382d5ee119fe706f441695435ac2043f7e5aaad02e";
  modelImageRevision = "4c70b98994f9f81e716e037e4e255c9aa8b3401a";
}
