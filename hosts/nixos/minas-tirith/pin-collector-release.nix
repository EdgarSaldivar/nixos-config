{
  # Staged until the manual publish workflow produces both immutable image
  # digests and a read-only GHCR pull credential has been added to SOPS.
  staged = false;
  enabled = false;
  registryPullSecretReady = false;
  gitRevision = null;
  apiImage = null;
  apiImageRevision = null;
  modelImage = null;
  modelImageRevision = null;
}
