# PinCollector: MinIO → Garage

MinIO's community edition was archived on 2026-04-25: no releases, no security fixes, and
nixpkgs now marks it insecure. PinCollector's object store moves to Garage v2.3.0
(Deuxfleurs). The app speaks plain S3 (path-style, SigV4 presigning since PinCollector
81803e6), so the move is configuration plus data, in the phases below.

Every command runs on pelargir as `K='sudo -n k3s kubectl -n pin-collector'`. Never print
Secret data.

## Phase A — Garage beside MinIO (this commit; no traffic change)

What deploys:

- `StatefulSet garage` on minas, one node, replication 1, SQLite metadata with
  `metadata_fsync` and `data_fsync`. PVCs `garage-meta` (2Gi) and `garage-data` (50Gi) on
  `local-path-retain`, so they sit in `/var/lib/rancher/k3s/storage` and ride minas' nightly
  ZFS copy like every other PVC.
- `Service garage` (S3, 3900) routes only to a ready node; `/health` is 503 until the layout
  is applied. `Service garage-admin` (3903, headless, publishes unready addresses) exists so
  the bootstrap can apply that layout.
- `Job garage-bootstrap-<api digest>-<hash>` runs `garage_bootstrap.py` in the API image:
  layout, key import (or proof that existing keys match their declared secrets), bucket
  `pin-collector-uploads`, app key read+write, backup key read only (never owner), then proves
  all of it through signed S3 calls. Its name hashes its own spec and script, so any change
  renders a new Job. The migration Job is named the same way now.
- `garage-ingress`: S3 from the app's pods and the storage/backup Jobs; admin only from the
  bootstrap.
- API, migration and training-pull pods also mount `garage-app-key-id`/`garage-app-secret`
  (unused until Phase C). The model service accepts presigned URLs from `minio` and `garage`.
- Suspended templates `storage-migrate-minio-to-garage` and `storage-migrate-garage-to-minio`.

### Durability (accepted residual risk)

`metadata_fsync = true` and `data_fsync = true` are the strongest settings Garage has. Data
blocks are fsynced before an upload is acknowledged; metadata is SQLite in WAL mode at
`synchronous=NORMAL`, which survives process crashes but can undo the most recent commits
on a sudden power loss or kernel crash. LMDB's equivalent (`MDB_NOMETASYNC`) has the same
property, and Garage exposes no fully synchronous mode. With one node there is no replica to
repair from. Consequence: uploads acknowledged in the last moments before a power cut can be
missing afterwards, while the app's database (PostgreSQL, fully synchronous) still references
them. Bounds: minas' nightly ZFS copy of the PVCs, the pelargir backup (Phase E, whose
reference check reports any such object), and Garage metadata snapshots every 6h. This is
the same class of exposure MinIO had here; accepted for a single-node home deployment.

Garage refuses secret files that are not 0600. Kubernetes projects Secrets root-owned 0440
with the pod's fsGroup, the only way a non-root pod can read them, so `garage.toml` sets
`allow_world_readable_secrets = true`; nothing outside the pod can read its secret tmpfs.

Check after deploy:

```sh
$K get pods -l app=pin-collector-garage
$K logs job/$($K get jobs -o name | grep garage-bootstrap | tail -1 | cut -d/ -f2)
```

The bootstrap log ends with `app key read/write/delete, backup key list/read only: proven
through S3`.

Rotating a Garage key is manual and ordered, so no client ever holds a key Garage does not
yet accept (a key's secret cannot change in place; rotation is a new key):
1. Generate the new pair locally (`GK` + 24 hex, and 64 hex). From a throwaway pod with the
   admin token, `ImportKey` it and `AllowBucketKey` the same permissions as the old key; prove
   it with a signed S3 read (and, for the app key, a write).
2. Only then put the new pair into `sops edit secrets/pin-collector.yaml` and deploy; the next
   bootstrap run finds the key present with a matching secret.
3. Once every client has restarted onto it, `DeleteKey` the old one.

## Phase B — copy and verify (repeatable, no effect on production)

```sh
# One operator runs storage-migrate at a time: it belongs to the cutover window, not to
# routine work. Within that, one fixed name for both directions: the API server refuses a
# second Job of the same name. A finished one is deleted in the foreground, so the name is
# only released once its pods are gone and no copy is still writing.
$K get job storage-migrate -o jsonpath='{.status.conditions[*].type}' 2>/dev/null
# Only if the line above says Complete or Failed:
$K delete job storage-migrate --cascade=foreground --wait --timeout=5m
$K get pods -l app=pin-collector-storage-migrate   # must list nothing
$K create job --from=cronjob/storage-migrate-minio-to-garage storage-migrate
$K wait --for=condition=complete --timeout=2h job/storage-migrate
$K logs job/storage-migrate --all-containers
```

Stages: `sync` makes Garage identical to MinIO (deletions included, `--metadata`), `compare`
lists keys whose size, Content-Type or user metadata differ, `repair` re-uploads those with
`--ignore-times` together with every key a `--download` byte comparison finds differing
or missing (rclone's sync skips byte-identical objects whose metadata changed, and can skip
a same-size object without comparable checksums), then byte-checks both directions again, `verify` compares again and fails on any
difference. Runs before cutover are rehearsals; only the quiesced run in Phase C counts.

Rehearsed locally on 2026-09-27 against MinIO and Garage 2.3.0 with the rendered scripts:
7 objects including a 12 MiB multipart upload and crop-evidence metadata; a stray
Garage-only object was deleted; a metadata-only change was caught by `compare`, fixed by
`repair`, and `verify` passed; the reverse direction passed.

## Phase C — cutover (short API outage)

1. Set `apiMaintenance = true` in `pin-collector-release.nix`; deploy. Wait for
   `$K wait --for=delete pod -l app=pin-collector-api --timeout=120s`. Confirm the
   model-service rollout from Phase A is complete. Do not start a training pull.
2. Run `storage-migrate-minio-to-garage` as in Phase B. It must complete.
3. Commit + deploy the switch, still in maintenance: API, migration and training-pull
   ConfigMaps get `PIN_COLLECTOR_UPLOAD_S3_ENDPOINT_URL: http://garage:3900` and their S3 key
   file paths point at `garage-app-key-id`/`garage-app-secret`. Confirm the new migration Job
   completes.
4. Set `apiMaintenance = false`; deploy.
5. Verify: `/ready`; open an existing scan image through the app; one new identify on the
   phone end to end; the new object is in Garage and not in MinIO.

Before C3 has deployed, MinIO is still authoritative: if the forward copy fails or is
abandoned, never sync Garage → MinIO. Set `apiMaintenance = false` and deploy (the API still
points at MinIO), then retry the forward copy later or leave Garage unused.

Rollback after C3 (the app has written to Garage): `apiMaintenance = true` deploy, then wait for
`$K wait --for=delete pod -l app=pin-collector-api --timeout=120s` exactly as in C1; run
`storage-migrate-garage-to-minio` under the same fixed Job name (so post-cutover writes, overwrites, deletions and metadata
carry back); deploy the revert still in maintenance; `apiMaintenance = false`; verify as C5.

## Phase D — MinIO retirement

Only after 7 days on Garage, a successful nightly backup, and a passed restore drill of a
post-cutover snapshot. MinIO is stale by design after cutover, so it is not a comparison
target.

1. Remove the MinIO key projections from API, migration and training-pull, the MinIO
   bootstrap init container, and `minio` from the model service's allowed hosts; deploy and
   confirm the rollout and the new migration Job.
2. Remove MinIO's StatefulSet, Service, NetworkPolicy, policy ConfigMap, both
   storage-migrate templates and the MinIO Secret keys (sops, applier); deploy. Addons are
   not pruned: `kubectl delete` those objects by name.
3. Last, delete the `minio-data` PVC and its retained PV.

## Phase E — pelargir backup

Designed with this migration (nightly restic of a pg_dump plus a mirror of Garage made with
the read-only backup key, every dump reference checked before a snapshot counts); built and
documented separately after cutover.
