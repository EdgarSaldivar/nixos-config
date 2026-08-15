{
  # Staged: PVCs are created and only PostgreSQL and MinIO start. The API and
  # model Deployments stay at zero replicas and the migration Job stays suspended
  # until the legacy data is restored under a separately authorized cutover.
  #
  # Images published by PinCollector run 31908779512, built from the reviewed
  # commit cb7e7cbe7524b46fa9b12074f1dcf0dfeb209e54. That commit is the second
  # parent of PinCollector master merge b396677; the images were built from the
  # commit itself, not the merge, so the revisions below are the commit SHA.
  # Both OCI revision labels and the API's baked /app/backend/.build-sha were
  # inspected on the published artifacts and equal that revision.
  staged = true;
  enabled = false;
  registryPullSecretReady = true;
  gitRevision = "cb7e7cbe7524b46fa9b12074f1dcf0dfeb209e54";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:c8232b0a91d3365e7173f014d4ead5192d88282c5a1140f0d9a203bd19bf404d";
  apiImageRevision = "cb7e7cbe7524b46fa9b12074f1dcf0dfeb209e54";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:77cd5346a10c870d712718055e39216f2b744413ba093e1cdfd1417bf5f4f3ff";
  modelImageRevision = "cb7e7cbe7524b46fa9b12074f1dcf0dfeb209e54";
}
