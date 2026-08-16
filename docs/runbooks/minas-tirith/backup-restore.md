# minas-tirith — backup and restore

Consolidated from RESTORE-RUNBOOK.md, NEXTCLOUD-ROLLBACK-RUNBOOK.md and
K3S-QUIESCE-DESIGN.md on 2026-08-16. The 39-container Docker restore those files
were built around is obsolete — the fleet runs on k3s and docker is at zero
containers — but the database mechanics below are live and still cited from
`backup-root-data.nix` and from the manifests.

---

## ⛔ TimescaleDB: `pg_dumpall` IS NOT A VALID BACKUP — tested 2026-08-08

`media-tracearr-1` embeds Postgres 15 with **timescaledb 2.24.0** and
**timescaledb_toolkit 1.22.0** (2 hypertables, 5 continuous aggregates). The generic
`pg_dumpall` this repo's backup script takes for every other Postgres **cannot be
restored**, and it fails in the way that matters: `psql` still exits 0.

Measured, restoring a `pg_dumpall` into a container built from tracearr's own image:

```
48 x ERROR: chunk not found
 5 x ERROR: ONLY option not supported on hypertable operations
 2 x ERROR: cannot copy to view
 4 x ERROR: insert or update ... violates foreign key constraint
     exit=0          <- the trap
```

Base tables land; every hypertable, continuous aggregate and `time_bucket` view does
not. A dump that restores "successfully" while silently dropping the time-series half of
the database is worse than no dump, because it looks like a backup.

### The procedure that DOES work (verified end to end)

Take a **per-database** dump in custom format, not `pg_dumpall`:

```sh
pg_dump -U tracearr -Fc -d tracearr > tracearr.dump
```

Restore with TimescaleDB's required dance:

```sh
psql -U tracearr -d postgres -c "CREATE DATABASE tracearr;"
# ⛔ PIN THE EXTENSION TO THE DUMP'S VERSION. Creating it unpinned installs whatever the
# image ships today (2.28.3), and post_restore then fails with
#   catalog version mismatch, expected "2.28.3" seen "2.24.0"
psql -U tracearr -d tracearr -c "CREATE EXTENSION timescaledb VERSION '2.24.0';"
psql -U tracearr -d tracearr -c "SELECT timescaledb_pre_restore();"
pg_restore -U tracearr -d tracearr tracearr.dump
psql -U tracearr -d tracearr -c "SELECT timescaledb_post_restore();"
```

Result: all application rows restored (servers 1, settings 15, server_users 20,
plex_accounts 1) **and 2 hypertables + 5 continuous aggregates**, matching live exactly.

Three residual `pg_restore` errors are EXPECTED and benign — `timescale_metadata already
exists` and `multiple primary keys for timescale_metadata`, both because
`CREATE EXTENSION` already built the extension's own catalog. Do not chase them.

⚠️ The third one is NOT cosmetic: `COPY failed for continuous_aggs_materialization_ranges
... violates foreign key constraint`. The aggregate DEFINITIONS restore, but their
materialization bookkeeping does not, so after a restore run
`CALL refresh_continuous_aggregate('<view>', NULL, NULL);` for each of the 5 or the
aggregates will read as empty until the next scheduled refresh.

### Why the extension version differs from the image

The live database's extension was created at 2.24.0 and never `ALTER EXTENSION ... UPDATE`d,
while the image now ships 2.28.3 and loads the matching versioned `.so` per catalog
version. The running container and the `:supervised` tag are the SAME image — so this is
not tag drift, it is a database that was initialised under an older extension. Read the
version from the source before restoring:

```sh
psql -U tracearr -d tracearr -t -A -c "SELECT extversion FROM pg_extension WHERE extname='timescaledb';"

---

# Quiescing a live database for backup

**Status: LIVE.** Applied to `jellyfin` since 2026-08-07 and running unattended.
Cited from `backup-root-data.nix`, `manifests/jellyfin.yaml` and `manifests/jellyfin-quiesce.yaml`.

## The problem

`jellyfin` is the only service whose database cannot be dumped while it runs: `library.db`
is in **rollback-journal mode** (not WAL) and jellyfin holds it write-locked for its entire
runtime. A 60 s busy timeout was tried and still failed. Under docker the backup script
does `docker stop jellyfin`, dumps, restarts, with a trap and a CRITICAL marker if the
restart fails.

That does not translate, because **minas is a k3s AGENT node with no kubeconfig and no
API access at all** (verified: `k3s kubectl get nodes` fails). The backup script cannot
scale a Deployment. `crictl stop` is not viable either — the kubelet restarts the
container immediately, so the dump would race an uncontrolled restart.

## ⛔ Why the obvious design is wrong

The intuitive answer is a CronJob that scales the Deployment to 0, dumps, scales back.
**Do not do this.** If that job is killed after scaling down — SIGKILL, node failure,
OOM — `replicas=0` persists **forever**, and Kubernetes treats it as the correct desired
state. A shell trap does not run on SIGKILL.

This is strictly worse than the docker version it replaces: there, a crashed script leaves
a stopped container that the heartbeat notices and complains about. Here the controller
would *actively maintain* the outage. Any scale-to-zero design needs an independent
deadman to be safe, which is a second mechanism to get right.

## The design that works

Delete a **pod**, never change desired replicas.

1. Run the workload as a **single-replica StatefulSet**, pinned to minas.
2. A **CronJob** deletes the predictably-named pod (`<name>-0`).
3. The StatefulSet controller recreates it. **Desired replicas is always 1**, so a job
   killed at any point is self-healing — there is no state that expresses a durable outage.
4. The pod's **initContainer** takes the backup. It runs before the app container, so the
   writer is absent *by construction* rather than by stopping anything.

### RBAC — and why StatefulSet specifically

```yaml
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "delete"]
  resourceNames: ["<name>-0"]
```

A StatefulSet's **stable pod name** is what makes `resourceNames` expressible. A
Deployment's random suffix would force `list` + `delete` across the namespace.

Verified with `kubectl auth can-i --as=system:serviceaccount:...`:

| capability | result |
|---|---|
| `delete pod/<name>-0` | **yes** |
| `delete pod/<other>` in another namespace | no |
| `delete pods` namespace-wide | no |
| **`patch deployments/scale`** | **no** |
| `list pods` | no |

No credential leaves the cluster, and no scale permission exists anywhere.

## The initContainer contract

- Dump to `.tmp`, validate, then promote atomically. Never overwrite a good dump with an
  unvalidated one.
- On failure: **preserve the previous dump**, write a durable status marker, and
  **exit 0**. Availability must not be held hostage to its own backup. Proven: with the
  backup forced to fail, the app container still reached `Running`/`ready` and the prior
  good dump was untouched.
- ⛔ **Run the container as ROOT and drop to the database uid only for `sqlite3`, via
  `setpriv`** — do NOT run the whole init container as that uid. Cross-review caught the
  difference and it matters: the dump directory is root-owned, so a wholly-unprivileged
  init cannot promote or `chown` the artifact, and making that directory writable by an
  application uid weakens the backups it is meant to protect. This mirrors exactly what
  `backup-root-data.nix` already does for the docker path, and for the same reason — root opening a
  WAL database creates root-owned `-wal`/`-shm` the app then cannot open.
- **Validation is three parts**: `integrity_check` = ok **AND** table count > 0 **AND**
  bytes above a floor. An earlier draft of this document demanded a byte floor while the
  sqlite code checked only the first two; both now agree. The forced-failure run produced
  `ok=ok tables=0 bytes=4096`, which is why all three are needed.

## The marker contract, and how it reaches the heartbeat

The initContainer writes **`/storage2/backup/staging/<name>/.status`** — the service's own
staging tree, **not** the shared dump directory. Moved there when jellyfin was built, on
cross-review, for two independent reasons:

- **Trust.** The capture runs as root. Mounting the shared `dumps` directory into it would
  give that container write access to *every* service's dump — enough to delete them, or
  to forge a `success` marker for a backup that never ran. It now reaches nothing but its
  own tree.
- **Availability.** A `hostPath` the kubelet cannot resolve is rejected **before** the init
  script runs, so the fail-and-exit-0 contract below cannot rescue it. Every mount the pod
  needs must therefore be one the backup can lose without taking the *service* down. Losing
  the staging mount degrades the backup — the marker and the staged database both go
  missing, loudly — instead of refusing to start jellyfin.

```
success <iso8601> tables=<n> bytes=<n>
FAILED  <iso8601> <reason>
```

`backup-root-data.nix` consumes it (implemented, all branches tested). Two properties are
load-bearing:

- The expected set is **DECLARED**, not inferred from which markers exist. A job that
  never runs writes no marker; inferring would read that as success forever.
- Success must **PARSE**, not merely fail to say FAILED. An empty or truncated marker is
  what a crash actually produces, and the first implementation accepted it. It now
  requires `^success ... tables=<positive> bytes=<above 4096>`.

## Workload requirements the pattern imposes

Beyond the manifest invariants in `docs/architecture/k3s.md`:

- **`serviceName` + a governing headless Service** — a StatefulSet requires it — *plus* a
  normal ClusterIP Service for consumers and the traefik route. Two Services, not one.
- **No `strategy: Recreate`.** That is Deployment syntax; StatefulSets replace the single
  ordinal serially and do not take it.
- **`automountServiceAccountToken: false`** on the workload Pod. Only the CronJob needs API
  credentials; the application must not carry a token it has no use for.
- **Suspend the CronJob while the workload is staged at `replicas: 0`**, or it will run and
  fail against a pod that does not exist, before cutover has even happened.
- **`concurrencyPolicy: Forbid`**, `--wait=false`, a **digest-pinned** kubectl image, and
  resources on the job as well as the app.
- The init image must be **built by this repository** (`dockerTools` is the natural fit)
  containing sqlite3, `setpriv`, coreutils, grep and a shell — and referenced by digest.
  Do not use an arbitrary public convenience image, and do not install packages at pod
  start; the proof-of-concept used `apk add`, which needs network during startup and is
  unacceptable for a real workload.


## The marker contract, and how it reaches the heartbeat

The initContainer writes **`/storage2/backup/staging/<name>/.status`** — the service's own
staging tree, **not** the shared dump directory. Moved there when jellyfin was built, on
cross-review, for two independent reasons:

- **Trust.** The capture runs as root. Mounting the shared `dumps` directory into it would
  give that container write access to *every* service's dump — enough to delete them, or
  to forge a `success` marker for a backup that never ran. It now reaches nothing but its
  own tree.
- **Availability.** A `hostPath` the kubelet cannot resolve is rejected **before** the init
  script runs, so the fail-and-exit-0 contract below cannot rescue it. Every mount the pod
  needs must therefore be one the backup can lose without taking the *service* down. Losing
  the staging mount degrades the backup — the marker and the staged database both go
  missing, loudly — instead of refusing to start jellyfin.

```
success <iso8601> tables=<n> bytes=<n>
FAILED  <iso8601> <reason>
```

`backup-root-data.nix` consumes it (implemented, all branches tested). Two properties are
load-bearing:

- The expected set is **DECLARED**, not inferred from which markers exist. A job that
  never runs writes no marker; inferring would read that as success forever.
- Success must **PARSE**, not merely fail to say FAILED. An empty or truncated marker is
  what a crash actually produces, and the first implementation accepted it. It now
  requires `^success ... tables=<positive> bytes=<above 4096>`.

