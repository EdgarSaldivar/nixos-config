# nextcloud — quiesced rollback artifacts, and how to restore from them

Built **2026-08-09T09:30:59Z**. This is the rollback that the nextcloud section of
`K3S-HANDOFF.md` requires to exist before `replicas: 1`, and that cross-review rejected
three earlier scopes for lacking.

⚠️ These artifacts are a **point-in-time from 2026-08-09T09:30:59Z**, not from the cutover
moment. Docker was restarted immediately after they were built, so anything written to
nextcloud after that timestamp is NOT in them. Re-take them at the actual cutover — the
whole capture took **under four minutes**, and the snapshot is free.

---

## The artifacts

| artifact | where | covers |
|---|---|---|
| ZFS snapshot | `storage2@nextcloud-quiesced-20260809T093059Z` | `/storage2/nextcloud/data` (1.5 TB) **and** `/storage2/nextcloud/db` (PGDATA) |
| app-tree archive | `/storage2/backup/nextcloud-precutover-20260809T093059Z/app-tree.tar.gz` (206 MB, 28245 entries) | `/usr/local/lib/docker-nextcloud` — the 680 MB app tree **including `config.php`** |
| database dump | same dir, `nextcloud-db.dump` (12.7 MB, `pg_dump -Fc`) | logical copy of database `nextcloud-db` |
| identity baseline | same dir, `identity-baseline.txt` | what a restore must be checked *against* |
| checksums | same dir, `SHA256SUMS` | integrity of the three files above |

```
d5ddf41586a07b9944991cc7710b770167dec8bc8f669714de6a0d2881df40e6  app-tree.tar.gz
392d13fd8ee88a58f93029b235108ae44ce8a4e7154f28566718f7a62d01aa80  nextcloud-db.dump
18cafb951e4b498a4c74a5259286117c808fdf8eb4cca5f49e014b0d855035c4  identity-baseline.txt
```

## ✅ Why these three are COHERENT — the property the 08:40 snapshot lacked

The earlier `storage2@nextcloud-precutover-20260809T084029Z` was taken with all three
containers **running**, and its database dump was taken separately. This set was built in
one quiesced window, in this order, and each step was gated rather than assumed:

1. `occ maintenance:mode --on` → verified `maintenance: true`.
2. `docker stop` app, then redis → both **`Exited (0)`**.
3. `pg_dump -Fc` **while postgres still ran with no writers left** → rc=0.
4. `docker stop -t 120 nextcloud-db` → exit 0 in **0.255 s**.
5. **GATE**: `pg_controldata` read `Database cluster state: **shut down**` — not
   `in production`, which is what `readmeabook` got and what forces WAL-replay planning.
   Checkpoint `4/275F04A0`, system identifier `7147093535374221351`.
6. **GATE**: `postmaster.pid` absent, **0** open file handles under either path
   (`lsof`), all three containers `Exited (0)`.
7. `zfs snapshot` — instant — then `tar` the app tree. Nothing could write between them.

**The coherence is verified, not asserted**: `pg_controldata` run against the *snapshot's
own copy* of PGDATA reads `shut down` at the identical checkpoint `4/275F04A0`. The
snapshot therefore contains a cleanly-stopped cluster, not a torn one.

✅ The dump is **restore-TESTED**, which is what makes it a backup: restored into an
isolated `postgres:14.5` (`--network none`, tmpfs PGDATA) with **zero diagnostic lines**,
not merely exit 0. Every identity matched the baseline — 4 users incl. `edgar`/`Edgar`,
5 storage id strings, and per-storage counts **and** bytes; 103 tables, 124415 filecache
rows.

⚠️ The archived `config.php` carries `'maintenance' => true`, because maintenance mode was
enabled before the archive was taken. That is a **safety property, not a defect** — a
restored app tree comes up sealed. Finish any restore with `occ maintenance:mode --off`.

⚠️ The per-storage `bytes` in the baseline **double-count**: nextcloud stores aggregated
sizes on directory rows, so the sum exceeds the real 1.5 TB. It is a deterministic
*fingerprint* for comparison, not a byte total. Do not "fix" it into a disk-usage figure.

## ⛔ What these artifacts do NOT protect against

They are on the **same pool** (`storage2`, `encryption=off`) as the data. This is not a
backup: no protection against disk failure, pool loss, or the building burning down. They
protect against exactly one thing — **this migration going wrong** — which is the risk
that was blocking. `/storage2/nextcloud/data` still has no off-host copy.

---

## RESTORE

### ⛔ Never `zfs rollback`

`/storage2/nextcloud` is a **directory inside the `storage2` root dataset**, not a dataset
of its own (verified: `zfs list -r storage2` shows only `backup`, `backups`,
`pincollector*`). So `zfs rollback storage2@nextcloud-quiesced-…` reverts **the entire
2.83 TB root dataset**, including `/storage2/backup-2026-07-30` and every unrelated tree.
That is the blast radius that got an earlier scope rejected.

Restore **selectively, reading from the snapshot directory**. No clone is needed — `.zfs`
is readable directly and read-only, so there is no clone lifecycle to leak:

```
/storage2/.zfs/snapshot/nextcloud-quiesced-20260809T093059Z/nextcloud/{data,db}
```

(If a *writable* staging copy is wanted instead:
`zfs clone storage2@nextcloud-quiesced-20260809T093059Z storage2/restore-nextcloud`, and
`zfs destroy` it afterwards. The read-only path is preferred.)

### Step 0 — ⛔ the cross-runtime interlock, BEFORE anything else

`strategy: Recreate` prevents overlapping *Kubernetes* revisions only. It cannot stop a
retained Docker container from reopening the same PGDATA. Two postgres processes on one
PGDATA is how the database is lost.

```sh
# 1. scale BOTH k3s workloads to zero DECLARATIVELY, app first, then database
ssh pelargir 'sudo k3s kubectl -n nextcloud scale deploy/nextcloud    --replicas=0'
ssh pelargir 'sudo k3s kubectl -n nextcloud scale deploy/nextcloud-db --replicas=0'

# 2. PROVE termination — not "no output", an actually empty list
ssh pelargir 'sudo k3s kubectl -n nextcloud get pods'

# 3. neutralise auto-deploy or it re-asserts the manifest and resurrects the Pods.
#    Per K3S-HANDOFF.md rollback ordering: auto-deploy -> deployments -> k8s-*.yml routes.
```

⚠️ If the manifest was committed at `replicas: 1`, scaling is temporary — the deploy
controller reasserts it on the next checksum change or k3s restart. Revert the manifest.

### Step 1 — prove nothing holds the paths

```sh
ssh minas 'sudo docker ps -a --filter name=nextcloud --format "{{.Names}} {{.Status}}"
           sudo lsof 2>/dev/null | grep -cE "/storage2/nextcloud|/usr/local/lib/docker-nextcloud"'
```

Want: all containers `Exited`, handle count **0**.

### Step 2 — restore the app tree

```sh
SNAP=nextcloud-quiesced-20260809T093059Z
DIR=/storage2/backup/nextcloud-precutover-20260809T093059Z
ssh minas "sudo sh -c 'cd $DIR && sha256sum -c SHA256SUMS'"

ssh minas 'sudo mv /usr/local/lib/docker-nextcloud /usr/local/lib/docker-nextcloud.pre-restore'
ssh minas "sudo tar --numeric-owner --acls --xattrs -xzf $DIR/app-tree.tar.gz -C /usr/local/lib"
```

⛔ `--numeric-owner` is required — the tree carries its own numeric ownership and must not
be remapped by the restoring host's name lookups.

### Step 3 — restore the database

**Preferred — the PGDATA copy from the snapshot** (byte-identical, cleanly shut down):

```sh
ssh minas 'sudo mv /storage2/nextcloud/db /storage2/nextcloud/db.pre-restore
           sudo rsync -aHAX --numeric-ids \
             /storage2/.zfs/snapshot/nextcloud-quiesced-20260809T093059Z/nextcloud/db/ \
             /storage2/nextcloud/db/'
# GATE before starting anything:
ssh minas 'sudo docker run --rm --user 0 --entrypoint pg_controldata \
  -v /storage2/nextcloud/db:/d:ro postgres@sha256:135c62a8134dcef829a1e4f5568bfae44bcfa2c75659ff948f43c71964366aa4 \
  -D /d | grep -Ei "cluster state|system identifier"'
```

Want `shut down` and identifier `7147093535374221351`. ⛔ A *different* identifier means
you are looking at a freshly-initialised cluster — stop, do not start nextcloud over it.

**Fallback — the logical dump**, if PGDATA is damaged rather than merely wrong. Restore
`nextcloud-db.dump` with `pg_restore` into a cluster initialised with the same role, then
compare against `identity-baseline.txt`.

### Step 4 — restore the 1.5 TB data directory ONLY if it was actually changed

⛔ **Read this before running it.** `--delete` is what makes it a true point-in-time
restore, and it therefore **destroys every file uploaded since 09:30:59Z**. That is the
semantics of a rollback, and it is a decision, not a step.

```sh
# ALWAYS dry-run first and read the delete list.
ssh minas 'sudo rsync -naxHAX --numeric-ids --delete \
  /storage2/.zfs/snapshot/nextcloud-quiesced-20260809T093059Z/nextcloud/data/ \
  /storage2/nextcloud/data/ | head -100'
```

In most rollbacks this step is **unnecessary**: a failed cutover usually leaves the files
untouched and only the database and app tree need reverting. Nextcloud will not have
deleted user files by itself. Prefer restoring nothing here over restoring everything.

### Step 5 — restart docker and verify

```sh
ssh minas 'sudo docker start nextcloud-db'   # wait for pg_isready
ssh minas 'sudo docker start nextcloud nextcloud-redis'
ssh minas 'sudo docker exec -u www-data nextcloud php occ maintenance:mode --off'
ssh minas 'curl -s -o /dev/null -w "%{http_code}\n" https://drive.saldivar.io/'   # want 302
```

⛔ **`docker start`, never `docker compose up -d`.** Compose rebuilds the container from
today's file, applying every drift accumulated since it was created — that is how
`btbooks` broke on 2026-08-07. Restart policy on all three is `no`; `docker start`
preserves it.

⛔ Run the ingress check **from minas**. The Mac and pelargir cannot reach public ingress
(hairpin NAT), so from there a healthy service reads as a total outage.

### Step 6 — verify against IDENTITY, not counts

Counts alone are not an acceptance test — a different database can also hold 124415 rows.
Compare against `identity-baseline.txt`: user *ids and display names*, storage *id
strings*, and per-storage counts **and** bytes.

⛔ **Never use `occ files:scan --all` as validation.** It mutates the structure being
compared.

---

## Lifecycle — ⛔ do not let the snapshot linger

It costs `0B` today and grows with every block the live data overwrites, on a 1.5 TB
dataset. `storage2@nextcloud-precutover-20260809T084029Z` (the older, torn one) is
superseded by this set and can go now.

```sh
ssh minas 'sudo zfs destroy storage2@nextcloud-precutover-20260809T084029Z'   # superseded
ssh minas 'sudo zfs destroy storage2@nextcloud-quiesced-20260809T093059Z'     # ONLY once accepted
```

⚠️ The rollback window stays open only as long as the snapshot exists. Destroying it is
the act that makes the migration final.
