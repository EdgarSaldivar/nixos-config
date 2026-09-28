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

Every "deploy" below is one merged PR, then a switch from `origin/master` (AGENTS.md §1):
maintenance on, the switch, and maintenance off are three PRs. (The 2026-09-28 cutover
predates that rule: it was deployed commit by commit from a branch and merged afterwards.)

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

## Phase E — nightly backup to pelargir

### What runs

CronJob `backup` in `pin-collector` (`manifests/pin-collector.yaml.in`), daily at 03:30
UTC (`timeZone: Etc/UTC`), `concurrencyPolicy: Forbid`. Its pod runs on pelargir only
(nodeSelector plus a toleration for the control-plane taint), under Restricted Pod Security
as 10001, with every image pinned by a multi-arch digest (pelargir is arm64). Its scripts
are `backup_*` beside the manifest, shipped in a ConfigMap named by their content.

First the native sidecar `lock` (`backup_lock.py`, an init container with
`restartPolicy: Always`) runs for the pod's whole life: it takes `flock` on `.lock` in the
backup root and writes `acquired`, `busy` (another pod holds it), `wrong-target` or `error`
to a shared memory file; its startupProbe holds the stages back until that is written. Then
the stages, each a container that must succeed before the next starts, and each refusing
unless the lock state is exactly `acquired` and the backup root is the prepared directory:

1. `meta-1` (rclone): list the bucket with metadata into `work/objects-1.json`.
2. `copy-1` (rclone): `rclone copy` Garage into `mirror/`. Copy never deletes.
3. `pg-dump` (the live server's `pgvector` image): `pg_dump -Fc` into
   `work/db/pin_collector.dump`, prove it with `pg_restore --list`, and extract every object
   key the dump references (`uploads/<user>/<name>`, `internal/golden-truth/<catalog>/<name>`,
   `internal/crop-evidence/<sha256>/<name>`) into `work/refs.txt`.
4. `meta-copy-2` (rclone): list and copy again, so objects written during the dump are in.
5. `check` (python, `backup_check.py`): merge both listings into the cumulative
   `work/objects.json` (keyed by key, newest listing wins; it carries each object's
   Content-Type and user metadata, which the mirror's plain files do not), then fail if any
   key in `refs.txt` is not a file in `mirror/` or has no valid entry in `objects.json`
   (an object whose `Path` is its key, with a `Metadata` object and a Content-Type). Listing
   records that are not valid are reported and never merged, so they never replace a valid
   entry. At its start it prunes entries whose key is neither in `mirror/` nor referenced
   (the previous run's `mirror-prune` deleted it, after that run's snapshot held it).
6. `restic` (restic): `init` once, otherwise `unlock` (stale repository locks only, see
   below); `backup --json --host pincollector-pelargir work mirror`. Only a snapshot restic
   finished cleanly (exit 0) is tagged `complete`; an incomplete one (exit 3, a file could
   not be read) is forgotten at once and the stage fails. Then
   `forget --tag complete --keep-daily 14 --keep-weekly 8 --keep-monthly 6 --prune`, `check`
   (plus `--read-data-subset=1/4` on the 1st of the month).
7. `mirror-prune` (rclone): `rclone sync` Garage into `mirror/`, so deletions reach the
   mirror only after a snapshot holds the deleted object, and record the mirror's keys in
   `work/mirror-keys.txt` (carried by the next snapshot).

Any failure fails the pod; the Job retries with a fresh pod from stage 1 up to three
attempts (`backoffLimit: 2`), which also covers an upload caught between its row and its
object. `activeDeadlineSeconds` is 4h. Only Garage's read+list backup key is mounted: the
backup cannot change the bucket. The CronJob is suspended while the release is not enabled.

`concurrencyPolicy: Forbid` stops only the CronJob's own Jobs from overlapping, and the
volume admits any number of pods on pelargir, so the lock above is what keeps a manual Job
and the scheduled one apart. It is a kernel lock held by an open file: it ends with the
pod, however the pod ends, so it never goes stale and never needs clearing. Never delete
`.lock`: a running pod keeps its lock on the deleted file, and the next pod would lock a new
file beside it, so both would run. The Job sets
`podReplacementPolicy: Failed`, so a retry pod starts only after the failed one has fully
terminated and released it.

`restic unlock` without `--remove-all` removes only stale repository locks (restic refreshes
a live one every few minutes), such as one a killed backup pod left. The pipeline lock means
no other backup is running at that point; a restore drill's live lock is left alone.

Retention has no hard maximum age: 14 daily, 8 weekly and 6 monthly `complete` snapshots,
nominally about six months (restic also keeps the oldest snapshot while the policy is not
yet full). A snapshot without the tag (a pod killed between `backup` and `tag`) is outside
the policy and stays until removed by hand: `restic snapshots` lists it, `restic forget <id>`
removes it.

A deploy that changes a backup script renames the scripts ConfigMap and deletes the old
one. A backup Job running at that moment keeps its pod, but a retry pod of that Job can no
longer mount the old ConfigMap and fails: that night's run fails, the next night's Job uses
the new ConfigMap. Accepted rather than keeping old ConfigMaps around.

### Where it lives

`/var/lib/pincollector-backup` on pelargir's root filesystem, through the static local PV
`pin-collector-backup` (Retain) and PVC `backup-target`. `pelargir/pincollector-backup.nix`
prepares it before k3s starts: a real directory (no symlink, nothing mounted in it), 10001,
mode 2770, and the sentinel `.pincollector-backup-target`, rewritten on every run with the
directory's `device:inode` (`stat -c %d:%i`). Every stage compares that with
`stat -c %d:%i /backup` (a bind mount keeps both) and refuses on any difference, so a
sentinel left behind on some other directory, or a PV pointed elsewhere, stops the run. Inside: `restic/` (the repository), `work/` and `mirror/` (the live staging the
snapshots are taken from). The restic password is `backup_restic_password` in
`secrets/pin-collector.yaml` (Secret key `backup-restic-password`); without it the
repository is unreadable.

It is one copy, on a different machine from the data. pelargir's own restic backup to minas
(`pelargir/backup.nix`) covers `/var/lib/restic-staging/pelargir` and the k3s PVC storage,
not this directory, so losing pelargir loses this repository (minas still holds the live
data); an off-site copy is not built.

### Manual run and status

```sh
$K create job --from=cronjob/backup backup-manual-$(date -u +%Y%m%d%H%M)
$K wait --for=condition=complete --timeout=4h job/backup-manual-<stamp>
$K logs job/backup-manual-<stamp> --all-containers
$K get cronjob backup -o jsonpath='{.status.lastSuccessfulTime}{"\n"}'
$K get jobs --sort-by=.metadata.creationTimestamp | grep '^backup-'
```

A manual Job started while another backup is running fails at once: its `lock` sidecar logs
`backup lock: busy` and `meta-1` refuses with `lock state: busy`. That is the lock working;
wait for the other Job (`$K get pods -l app=pin-collector-backup`) and start it again.
`lock state: wrong-target` means pelargir's preparation unit did not run or the PV no longer
points at the prepared directory: check `systemctl status pincollector-backup-target` there.

A failed `check` prints each missing key. A key the database references that Garage no
longer has fails every run from the night after `mirror-prune` removed it: look it up in
earlier snapshots (`objects.json`, `mirror/`) before anything else.

### What alerts on failure

Nothing yet. minas' heartbeat (`minas-tirith/scripts/healthcheck-ping.sh`) does report
failed Job pods, but it finds them with `k3s crictl` against minas' own container runtime
(minas is an agent with no API access), so it only sees pods that ran on minas. The backup
pod runs on pelargir, and pelargir's `monitoring.nix` checks hardware and minas' ingress,
not Jobs. Until a check exists, read `lastSuccessfulTime` above; it should never be more
than a day old.

### Restore

Do it on pelargir, in a scratch pod that mounts the backup read-write only to restore into
`restore-drill/` (not snapshotted, not touched by the CronJob). The drill does not take the
pipeline lock; restic's own repository lock keeps `restore` and the nightly `forget --prune`
apart, but run it when no backup pod is running (and not around 03:30 UTC) so a prune does
not fail the night's backup. Restore only `complete` snapshots.

```sh
$K apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: backup-restore-drill
  namespace: pin-collector
  labels: { app: pin-collector-backup-restore-drill }
spec:
  nodeSelector: { kubernetes.io/hostname: pelargir }
  tolerations:
    - { key: node-role.kubernetes.io/control-plane, operator: Exists, effect: NoSchedule }
  restartPolicy: Never
  enableServiceLinks: false
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    fsGroup: 10001
    fsGroupChangePolicy: OnRootMismatch
    seccompProfile: { type: RuntimeDefault }
  containers:
    - name: restic
      image: restic/restic:0.18.1@sha256:39d9072fb5651c80d75c7a811612eb60b4c06b32ffe87c2e9f3c7222e1797e76
      command: [sleep, "86400"]
      env:
        - { name: RESTIC_REPOSITORY, value: /backup/restic }
        - { name: RESTIC_PASSWORD_FILE, value: /run/secrets/pin-collector/backup-restic-password }
        - { name: RESTIC_CACHE_DIR, value: /tmp/restic-cache }
        - { name: HOME, value: /tmp }
      securityContext: &drill { allowPrivilegeEscalation: false, capabilities: { drop: [ALL] } }
      volumeMounts:
        - { name: target, mountPath: /backup }
        - { name: tmp, mountPath: /tmp }
        - { name: restic-password, mountPath: /run/secrets/pin-collector, readOnly: true }
    - name: postgres
      # Throwaway server holding a FULL copy of production (users, credentials, sessions)
      # with trust auth. It must not listen on TCP: the namespace has no default-deny
      # NetworkPolicy, so a TCP listener would be reachable from any pod in the cluster.
      # listen_addresses= leaves only the Unix socket, which is all the drill uses.
      image: pgvector/pgvector@sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b
      args: [postgres, -c, "listen_addresses="]
      env:
        - { name: POSTGRES_HOST_AUTH_METHOD, value: trust }
        - { name: PGDATA, value: /pg/data }
      securityContext: *drill
      volumeMounts:
        - { name: target, mountPath: /backup, readOnly: true }
        - { name: pg, mountPath: /pg }
        - { name: pg-run, mountPath: /var/run/postgresql }
    - name: check
      image: python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
      command: [sleep, "86400"]
      securityContext: *drill
      volumeMounts:
        - { name: target, mountPath: /backup }
  volumes:
    - name: target
      persistentVolumeClaim: { claimName: backup-target }
    - { name: tmp, emptyDir: {} }
    - { name: pg, emptyDir: {} }
    - { name: pg-run, emptyDir: {} }
    - name: restic-password
      secret:
        secretName: pin-collector-runtime
        defaultMode: 0440
        items:
          - { key: backup-restic-password, path: backup-restic-password, mode: 0440 }
EOF
$K wait --for=condition=Ready pod/backup-restore-drill --timeout=5m
D="$K exec backup-restore-drill"
$D -c restic -- restic snapshots --host pincollector-pelargir --tag complete
$D -c restic -- restic restore latest --host pincollector-pelargir --tag complete \
  --target /backup/restore-drill
# Snapshot paths are absolute: the restored tree is /backup/restore-drill/backup/{work,mirror}.
$D -c postgres -- createdb -U postgres drill
$D -c postgres -- pg_restore -U postgres -d drill --no-owner --no-privileges --exit-on-error \
  /backup/restore-drill/backup/work/db/pin_collector.dump
```

Compare exact row counts of every table, restored vs production (production read-only,
SELECT only, inside its own pod). This comparison is a pass criterion only when nothing was
written to production between the snapshot and the query: run the drill right after a
manual backup and confirm no scans or sign-ins happened in between; then every count must
match. If anything may have been written, the comparison is informational only and cannot
pass or fail the drill (a difference cannot be told apart from restore loss); the drill then
rests on `pg_restore --exit-on-error` succeeding, the reference check, and the photo hashes.
Both queries must succeed and print rows before the comparison means anything:

```sh
COUNTS="SELECT table_schema||'.'||table_name, (xpath('/row/c/text()', query_to_xml(format(
  'select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text
  FROM information_schema.tables WHERE table_type='BASE TABLE'
  AND table_schema NOT IN ('pg_catalog','information_schema') ORDER BY 1"
$D -c postgres -- psql -U postgres -d drill -Atc "$COUNTS" > restored-counts.txt \
  && $K exec statefulset/postgres -- env PGOPTIONS='-c default_transaction_read_only=on' \
     psql -U pin_collector -d pin_collector -Atc "$COUNTS" > production-counts.txt \
  && [ -s restored-counts.txt ] && [ -s production-counts.txt ] \
  && { diff restored-counts.txt production-counts.txt && echo "row counts identical"; } \
  || echo "STOP: a count query failed or returned nothing, or counts differ (see above)"
```

Then re-run the reference check against the restored tree, feeding it the script from this
repository:

```sh
$K exec -i backup-restore-drill -c check -- python - --work /backup/restore-drill/backup/work \
  --mirror /backup/restore-drill/backup/mirror --no-lock \
  < hosts/nixos/minas-tirith/manifests/backup_check.py
```

`--no-lock` is for a restored copy only: the CronJob passes `--target` and `--lock-state`. It must report no
missing keys (it rewrites the restored `objects.json`, a scratch copy).

Spot-check a few photos byte for byte: the sha256 of the restored file must equal the live
object's in Garage (read through the API pod's own storage client; only hashes are printed,
and the S3 credentials are loaded from their files inside that process only):

```sh
KEYS="uploads/<a>/<b> uploads/<c>/<d> uploads/<e>/<f>"   # from restored objects.json
for k in $KEYS; do $D -c check -- sha256sum "/backup/restore-drill/backup/mirror/$k"; done
$K exec -i deploy/api -c api -- python - $KEYS <<'PY'
import hashlib, os, sys
for n in list(os.environ):
    if n.startswith("PIN_COLLECTOR_") and n.endswith("_FILE") and n[:-5] not in os.environ:
        os.environ[n[:-5]] = open(os.environ[n]).read().strip()
from app.core.config import get_settings
from app.storage.uploads import upload_storage_from_settings
st = upload_storage_from_settings(get_settings(), ensure_s3_bucket=False)
for k in sys.argv[1:]:
    o = st.get_upload(k)
    print(hashlib.sha256(o.data).hexdigest() if o else "MISSING", k)
PY
```

Clean up with `$K delete pod backup-restore-drill`, then on pelargir
`sudo rm -rf /var/lib/pincollector-backup/restore-drill`.

Drill record: 2026-09-28 22:07 UTC, snapshot `332772d0` (600.9 MiB, 124 files): restore
2 s; `pg_restore` clean; all 54 tables' row counts identical to production (drill run right
after the backup, no writes in between); reference check
108 keys / 111 objects / 0 rejected; 3 photos hash-identical to Garage. This is the passed
drill Phase D requires.

Putting objects back into Garage is `rclone copy` of the restored `mirror/` with the app key;
rclone then sets Content-Type from the file extension, and the user metadata
(`owner-user-id`, `golden-truth-catalog-id`, `crop-evidence-source-sha256`) must be
reapplied from `objects.json`. No tool does that yet.
