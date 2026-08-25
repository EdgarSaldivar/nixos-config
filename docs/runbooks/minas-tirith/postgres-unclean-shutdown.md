# PostgreSQL recovery after an unclean shutdown

The `immich-postgres14` and `nextcloud-db` Deployments use hostPath PostgreSQL 14
clusters. Their `verify-pgdata` init containers fail closed when a mount is empty,
wrong, or not recognisably a PostgreSQL 14 cluster: they require `PG_VERSION=14`, a
non-empty `global/pg_control`, `base/`, and a numeric database system identifier.

The init containers deliberately do **not** require a missing `postmaster.pid` or a
`shut down` control-file state. Each Kubernetes Deployment is a `Recreate` singleton,
but `Recreate` alone is not a writer mutex during replacement/termination overlap.
Each database opens `PGDATA/.postgres-writer.lock`, acquires a nonblocking exclusive
`flock`, and retains that same file descriptor across the pinned image entrypoint and
PostgreSQL for the server's entire lifetime. Contention exits 75 instead of starting
a second server. The kernel releases the lock automatically when the process exits;
the lock file itself remains in PGDATA permanently.

There is an additional first-rollout gate because the incumbent Pods predate the lock
and therefore do not participate in it. While holding the lock, a new Pod checks the
durable `PGDATA/.postgres-writer-lock-adopted` marker. If the marker is absent, the Pod
requires both an absent `postmaster.pid` and `pg_controldata` state exactly `shut down`
before writing the versioned marker. Only then does it exec the entrypoint. Once the
marker exists and has the expected content, later starts still acquire the lock but
leave stale-PID and WAL recovery to PostgreSQL. This is what allows automatic crash
recovery after adoption without making the first replacement overlap an unwrapped
writer.

⛔ Do not remove or move `postmaster.pid` as routine crash recovery. PostgreSQL must
inspect it through its own startup path, including when it contains stale PID 1.
Do not remove `PGDATA/.postgres-writer.lock` while any database Pod may be running.
Removing a locked file unlinks its inode but does not release the holder's lock; a
replacement file at the same path would be a different inode and could admit a
second writer. Do not create, edit, remove, move, or replace
`PGDATA/.postgres-writer-lock-adopted` by hand.

## First lock adoption

Do not pre-create the adoption marker. On the first rollout, the normal safe path is:

1. The legacy Pod terminates cleanly.
2. The replacement opens and acquires the persistent writer lock.
3. With no `postmaster.pid` and control state `shut down`, it creates and verifies the
   versioned adoption marker while still holding the lock.
4. It execs the pinned entrypoint with the unchanged arguments, retaining the lock fd.

If the replacement reports `Refusing first lock adoption`, first allow an incumbent
that is still terminating to finish. Kubelet can retry the unchanged replacement; do
not delete either persistent file. If `postmaster.pid` remains or control state stays
`in production` after the incumbent is gone, the legacy writer stopped uncleanly.
The adoption gate intentionally will not perform that first crash recovery.

For that one-time case, leave the Deployment failed closed and prepare a separately
reviewed maintenance change. Docker and its unit are absent on Minas; do not invent a
Docker recovery command. The safe sequence is:

1. Follow [Before an offline repair or restore](#before-an-offline-repair-or-restore)
   to declare the affected Deployment at zero replicas, deploy through Pelargir, prove
   the Pod and every PGDATA holder are gone, and take the matching ZFS snapshot.
2. In a temporary reviewed revision of the **same** Deployment, keep the writer-lock
   acquisition and exact pinned entrypoint arguments, but allow the absent-marker
   branch to exec PostgreSQL without creating the marker. Restore one declared replica
   and deploy that revision through Pelargir. Do not create a second recovery Pod.
3. Allow PostgreSQL to replay WAL, pass a real query, and reach readiness. Then declare
   the Deployment at zero replicas again, deploy, and wait for its clean 120-second
   termination path. Prove no process has PGDATA open, `postmaster.pid` is absent, and
   `pg_controldata` reports `Database cluster state: shut down`.
4. Restore the ordinary lock-adoption wrapper and one declared replica through the
   same reviewed deployment path. It now creates and verifies the marker itself while
   holding the lock, then starts PostgreSQL. Never create the marker by hand.

## Normal recovery

After the marker has been created by the adoption gate, an unclean stop can leave
`postmaster.pid` behind (including with PID 1) and `pg_controldata` reporting
`in production`. Those facts alone do not require manual repair. Allow the existing
Deployment to restart and watch the database container:

```sh
kubectl -n immich rollout status deploy/immich-postgres14 --timeout=10m
kubectl -n immich logs deploy/immich-postgres14 -c immich-postgres14 --tail=100

kubectl -n nextcloud rollout status deploy/nextcloud-db --timeout=10m
kubectl -n nextcloud logs deploy/nextcloud-db -c nextcloud-db --tail=100
```

During crash recovery, PostgreSQL can log `database system was interrupted`,
`redo starts at`, and `redo done at` before it becomes ready. The ten-minute bound
matches the database startup probes. Do not create a second recovery Pod or restart
the database repeatedly while WAL replay is making progress.

An exit code of 75 means the nonblocking writer lock found another holder. Do not
delete the lock file to make the replacement Pod start. Identify the Pod or process
still using that PGDATA and allow normal termination to release the lock; the
replacement can then acquire the same persistent lock file on its next start.

## If `verify-pgdata` fails

Read the init-container failure before doing anything to the data:

```sh
kubectl -n <ns> get pod -l app=<db> -o wide
kubectl -n <ns> logs pod/<db-pod> -c verify-pgdata --tail=100
```

A stale PID file or an `in production` cluster state no longer fails this gate. A
failure means one of the structural identity checks did not pass. Do not weaken the
gate, initialise the directory, or point it at another path to make the Pod start.
Confirm the owning manifest's hostPath and inspect the cluster read-only on Minas:

```sh
sudo test -f <PGDATA>/PG_VERSION
sudo cat <PGDATA>/PG_VERSION
sudo test -s <PGDATA>/global/pg_control
sudo test -d <PGDATA>/base
V=$(sudo cat <PGDATA>/PG_VERSION)
sudo nix shell nixpkgs#postgresql_$V -c pg_controldata <PGDATA>
```

Stop if the version is unexpected, `pg_control` is missing or unreadable, or the
system identifier is absent. Those are restore or corruption symptoms, not ordinary
crash recovery.

## If PostgreSQL itself still fails

Preserve the first failure evidence:

```sh
kubectl -n <ns> logs pod/<db-pod> -c <db-container> --previous --tail=200
kubectl -n <ns> logs pod/<db-pod> -c <db-container> --tail=200
kubectl -n <ns> describe pod/<db-pod>
```

Before any offline inspection, prove that no process holds this specific PGDATA.
Other PostgreSQL clusters legitimately run on Minas, so identify their data paths
instead of treating any `postgres` process as the writer in question:

```sh
sudo lsof +D <PGDATA>                 # must be empty
pgrep -a postgres                     # identify every reported cluster
sudo k3s crictl ps | grep -i postgres # identify every running container
```

Snapshot the pool that owns the affected directory before any approved repair:

```sh
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

# immich: /storage/immich-data/pgdata
sudo zfs snapshot storage@immich-pgdata-recovery-$STAMP

# nextcloud: /storage2/nextcloud/db
sudo zfs snapshot storage2@nextcloud-pgdata-recovery-$STAMP
```

Take only the matching snapshot. Do not run `pg_resetwal`, delete a PID file, or
start a hand-built PostgreSQL command from the host based only on these checks.
Retain the logs and control data and diagnose the specific PostgreSQL error first.

## Before an offline repair or restore

Set only the affected database Deployment to `replicas: 0` in its owning manifest,
deliver that manifest through Pelargir using the repository's rsync, native build,
commit, second-rsync, and switch sequence, then verify all three states:

1. The source and installed manifest both declare zero replicas.
2. The owning `minas-immich` or `minas-nextcloud` AddOn applied successfully.
3. The database Deployment reports zero replicas and its Pod is gone.

An imperative `kubectl scale` is immediate containment, not a durable gate: k3s
auto-deploy can reassert the manifest after a checksum change or server restart.
Follow [backup-restore.md](backup-restore.md) for a database restore. Restore the
desired replica count declaratively after the repair or restore has passed its
consumer-path checks.

## Verification

The database Deployment must become available and the application must recover; a
Running sandbox alone is insufficient:

```sh
kubectl -n <ns> get deploy/<db>
kubectl -n <ns> get pod -l app=<db>
kubectl -n <ns> rollout status deploy/<db> --timeout=10m
kubectl -n <app-ns> rollout status deploy/<app> --timeout=10m
```

Finish with the recorded ingress baseline:

```sh
python3 hosts/nixos/minas-tirith/scripts/ingress-acceptance.py \
  --baseline hosts/nixos/minas-tirith/baselines/minas-ingress-authentik-baseline-*.txt \
  --public-dns
```

Expect every baseline row to pass. The long-term prevention remains graceful
Kubernetes/PostgreSQL shutdown ordering before the ZFS pools disappear; that work is
tracked in `ROADMAP.md`.
