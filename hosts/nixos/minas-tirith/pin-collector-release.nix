{
  # Staged until the manual publish workflow produces both immutable image
  # digests and a read-only GHCR pull credential has been added to SOPS.
  staged = false;
  enabled = false;
  registryPullSecretReady = false;
  apiImage = null;
  modelImage = null;
}
