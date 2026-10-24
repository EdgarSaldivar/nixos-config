{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Images published by PinCollector run 40050790803 from reviewed merge commit
  # 1e7ddb5fc1c3357a43dda6a61fed3fe1bb21e3e3 (PRs 122-125: accounts wave 1 = Sign in with
  # Apple, password reset by emailed code, auth rate limits, legal and support pages,
  # signed-out mode; collector depth wave 2 = acquired date/place/price fields and CSV
  # export). Migrations 20261018_0047 (accounts, Apple, reset limits) and 20261018_0048
  # (collection acquisition); Alembic head becomes 20261018_0048. Account mail stays off
  # (no mail key mounted yet), so password reset answers 503 mail_unavailable.
  # Both OCI revision labels and the API baked build fingerprint (read from the published
  # image) were verified before rollout.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  gitRevision = "1e7ddb5fc1c3357a43dda6a61fed3fe1bb21e3e3";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:b5d9c2deeea540ea945d1603bf756ca29c2ad17296102ee251679087a1902d3e";
  apiImageRevision = "1e7ddb5fc1c3357a43dda6a61fed3fe1bb21e3e3";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:1b9b251c7d7f855ddbf727535ef1e76f641239e7800aa6ccc315573edc3a9869";
  modelImageRevision = "1e7ddb5fc1c3357a43dda6a61fed3fe1bb21e3e3";
}
