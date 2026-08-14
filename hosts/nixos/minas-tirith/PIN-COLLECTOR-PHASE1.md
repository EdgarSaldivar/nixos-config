# PinCollector Phase 1 on k3s

Status: declared and fail-closed. `pin-collector-release.nix` has separate `staged` and
`enabled` gates. No production activation or data restore is part of this change.

## Declared shape

- Namespace `pin-collector`, Restricted Pod Security.
- API Deployment at one replica on minas-tirith.
- pgvector PostgreSQL and MinIO StatefulSets with `local-path-retain` PVCs.
- Triton/model Deployment on minas-tirith with `runtimeClassName: nvidia`, one accounted
  `nvidia.com/gpu` request, a retained model cache, and no public Service.
- An API-image-digest-versioned migration/bucket-init Job; API startup migrations and seed ingestion are off.
- ClusterIP Services only. The minas Traefik file provider owns `pin.saldivar.io`; admin is
  `/admin` on the same origin.

The device plugin advertises three time-slices for Plex, Jellyfin, and PinCollector. This
only lets the scheduler account for all three; it does not increase VRAM, throughput, or
fault isolation.

## Secrets

`secrets/pin-collector.yaml` is encrypted to the admin and pelargir recipients only. The
legacy runtime values were relayed byte-for-byte through an SSH-to-SOPS pipe and compared
without printing them. Sops-nix renders each value to its own tmpfs file on pelargir.
`k3s-apply-secrets` passes those files to `kubectl create secret --from-file` and pipes the
generated object directly to `kubectl apply`, preserving arbitrary bytes without inserting
them into YAML, command arguments, logs, the Nix store, or persistent disk. The worker node
cannot decrypt the SOPS document.

Application secrets are mounted as files, with each workload receiving only the keys it
needs. MinIO root credentials are limited to MinIO and its bootstrap init container; the
API uses a separately encrypted identity restricted to `pin-collector-uploads`. Do not
replace file mounts with `secretKeyRef` env values: resolved env values persist in
containerd metadata. The MinIO client keeps its credential-bearing configuration in a
memory-backed `emptyDir` and removes it on every exit. The remaining key is
`ghcr_dockerconfigjson`, a compact Docker config JSON containing a read-only package token.
Provision it without a plaintext temporary file or shell argument, then set
`registryPullSecretReady = true` in `pin-collector-release.nix`.

## Enabling a release

After the PinCollector manual publish workflow completes, verify its API fingerprint and
both OCI revision labels match the reviewed commit, then set:

- `gitRevision` to the reviewed 40-character lowercase Git commit;
- `apiImage` and `modelImage` to the workflow's `ghcr.io/...@sha256:...` outputs;
- `apiImageRevision` and `modelImageRevision` to the independently inspected
  `org.opencontainers.image.revision` labels; both must equal `gitRevision`;
- `registryPullSecretReady = true` only after the encrypted key exists;
- `staged = true` to create retained PVCs and start only PostgreSQL and MinIO;
- `enabled = true` only after the old PostgreSQL and MinIO data has been restored under a
  separately authorized cutover. This unsuspends the migration Job and raises API/model
  replicas from zero to one.

Setting `staged = false` keeps the frozen manifest under management but renders both
StatefulSets and both Deployments at zero replicas with the migration Job suspended. This
preserves PVCs and prevents k3s from continuing to reconcile a stale live release.

The Nix assertions reject an enabled release without staging, or a staged release with a
missing registry gate, mutable image, wrong repository, malformed revision, or image
revision that differs from the reviewed commit. Rebuild pelargir first so namespace,
Secrets, PVCs, workloads, and migration land before minas publishes the Traefik route.

## Acceptance

Require the migration Job complete, all four workloads healthy, `/health` and `/ready` at
200, `/admin` redirecting to the session-aware `/admin/ui` entry point (and an
unauthenticated `/admin/ui` redirecting to `/admin/ui/login`), the exact two image IDs,
both OCI revision labels and workload annotations matching `gitRevision`, the API
fingerprint reporting that revision, the expected Alembic head, restored row/object
inventories, and a real GPU recognition while Plex and Jellyfin remain scheduled. Preserve
stopped Compose volumes and retained PVCs until a restore drill is accepted. Rollback,
restart, deployment, and production data mutation each remain separately authorized
actions.
