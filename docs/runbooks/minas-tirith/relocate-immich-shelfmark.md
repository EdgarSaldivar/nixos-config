# Relocating immich and shelfmark off `/home/edgar/git/docker`

**Status: EXECUTED 2026-08-16 for immich and shelfmark.** Both are live on the new
paths. The traefik pair remains — see the last section.

Kept because the traefik move follows the same shape, and because what the first
draft got wrong is recorded inline and is the useful part.

Executed result:

| | |
|---|---|
| immich | hostPath `/usr/local/etc/immich/config`, Pod 1/1, `immich.saldivar.io` 200 |
| shelfmark | hostPath `/usr/local/etc/shelfmark/config`, rollout ok, `books.saldivar.io` 200 |
| backup | `_usr_local_etc_shelfmark_config_users.db`, integrity ok, 6 tables; stale old artifact deleted |
| ratchet | 5 bindings → 2 |

⏳ The old source trees are **still in place** and are the rollback. Remove them only
after a full backup cycle, in a separate session.

## Why

`/home/edgar/git/docker` is the pre-migration Docker Compose checkout. Its
containers are gone; its *directory* is still the on-disk config store for live
workloads plus the ingress route drop point.
`checks/external-checkout-dependency.nix` pins the exact set — **five bindings**.
This removes three (two manifests and one backup declaration), leaving the traefik
pair, which must move together and touches ingress for 26 hostnames.

| workload | from | to |
|---|---|---|
| immich | `/home/edgar/git/docker/immich/config` (766 MB) | `/usr/local/etc/immich/config` |
| shelfmark | `/home/edgar/git/docker/books/shelfmark/config` (15 MB) | `/usr/local/etc/shelfmark/config` |

Target follows the convention nine other migrated workloads already use.

---

## ⛔ Preconditions — all three, before copying anything

**1. You can reach pelargir.** The `hostPath` lives in a manifest, and manifests
are delivered **only** by pelargir's auto-deploy directory. Rebuilding *minas*
changes nothing — that exact mistake caused the only outage of the migration.

```sh
ssh pelargir hostname     # must work. NOT reachable from a Mac off the tailnet,
                          # and minas cannot ssh there (no authorized key).
```

**2. Both paths are on the same filesystem.** The declared layout puts `/` and
`/home` both on ext4-on-LUKS on the Samsung NVMe (`disko.nix`), but an unmanaged
nested mount cannot be disproved from configuration alone:

```sh
findmnt -T /home/edgar/git/docker/immich/config
findmnt -T /usr/local/etc
```

**3. Backup coverage is unchanged.** Verified 2026-08-16; re-check if that list
changes. `backup-root-data.nix` walks `/etc /home /usr/local /opt /srv …`, so both
roots are already file-backup sources and the *filesystem* backup is unaffected.
Shelfmark's application-consistent SQLite dump is a separate mechanism and **does**
change — see S3/S4.

---

## The cutover is DECLARATIVE. Do not `kubectl scale`.

The first draft said to quiesce with `kubectl scale deploy/immich --replicas=0`.
That is wrong, for a reason this repository documents in
`docs/architecture/k3s.md`: **k3s re-applies a manifest on checksum change OR
server restart**, so a declared `1` is reasserted at an unpredictable moment.
Mid-copy, that restarts the workload against the *old* path while you are copying
it. `shelfmark.yaml` records this exact failure mode. I wrote that rule into the
architecture doc earlier the same day and then violated it in this runbook.

Each workload therefore moves in **two deploys**:

```
deploy A:  replicas: 0                   commit -> rebuild pelargir -> verify Pod gone
           final rsync + verify          nothing is writing now
deploy B:  new hostPath + replicas: 1    commit -> rebuild pelargir -> verify
```

Between A and B the workload is intentionally down. If B fails it stays down —
which needs intervention but is unambiguous, unlike an imperative zero that a
later checksum event silently reverses.

Do **immich first, completely**, before touching shelfmark.

---

## immich

### I1 — warm copy (optional, while running)

Shortens the outage. Stale by definition; re-synced in I3.

```sh
sudo mkdir -p /usr/local/etc/immich
sudo rsync -aHAX --numeric-ids \
  /home/edgar/git/docker/immich/config/ /usr/local/etc/immich/config/
```

`-aHAX --numeric-ids` preserves ownership, ACLs and xattrs. immich's containers run
as uid 0 and the manifest sets no `runAsUser`/`fsGroup`, so the copy must not
normalise ownership.

### I2 — deploy A: scale to zero, declaratively

Set `replicas: 0` in `manifests/immich.yaml`, commit, rebuild **pelargir**:

```sh
kubectl -n immich get pods            # must be empty before continuing
```

### I3 — final sync against a stopped workload

```sh
sudo rsync -aHAX --numeric-ids --delete \
  /home/edgar/git/docker/immich/config/ /usr/local/etc/immich/config/
# verify: the itemised dry run must report NOTHING
sudo rsync -aHAXn --numeric-ids --delete --itemize-changes \
  /home/edgar/git/docker/immich/config/ /usr/local/etc/immich/config/
```

⚠️ Itemised dry run, not `diff -r`. `diff -r` compares content only and is blind to
ownership, mode, ACLs and xattrs — exactly what `-aHAX` exists to carry.

Check for symlinks pointing back into the old tree:

```sh
sudo find /usr/local/etc/immich/config -type l -lname '/home/edgar/git/docker/*'
```

### I4 — deploy B: new path, back to one replica

In `manifests/immich.yaml` set the `config` volume to
`hostPath: { path: /usr/local/etc/immich/config, type: Directory }` and
`replicas: 1`. Remove the immich entry from `expected` in
`checks/external-checkout-dependency.nix` — **the build fails until you do**; that
is the ratchet working, not an obstacle. Commit, `nix flake check`, rebuild
**pelargir**.

### I5 — verify the SOURCE, not just the symptom

A Running Pod proves *a* directory mounted, not *which*.

```sh
kubectl -n immich get deploy immich -o jsonpath='{..hostPath.path}'; echo
kubectl -n immich rollout status deploy/immich --timeout=180s
curl -sS -o /dev/null -w '%{http_code}\n' https://immich.saldivar.io   # expect 200
```

⚠️ `immich.saldivar.io`, **not** `photos.saldivar.io`. The latter does not exist,
returns 404, and that 404 reads as a broken workload — an earlier draft named it
and I was fooled by it myself. Hostnames live in `traefik-hostnames.nix`.

Exercise **machine learning**, not just the HTTP ping: the 766 MB child is the
model cache, and silently re-downloading it is the plausible failure. Trigger a
search or face-detection job and check the log for cache reuse rather than fetch.

---

## shelfmark

Same shape, plus two differences that matter.

### S1 — ⛔ the live SQLite database makes a warm copy untrustworthy

`users.db` is written by a running app. rsync reads sequentially while a writer
changes pages, and can capture `-wal`/`-shm` mid-checkpoint. The result may be
corrupt — or worse, structurally valid and missing recent commits. This repository
already knows this: `backup-root-data.nix` uses SQLite's `.backup` API precisely
because a plain file copy interleaves old and new pages.

So the copy that counts is the one taken **after deploy A**, with the Pod gone.
Validate it before trusting it:

> **`sqlite3(1)` is NOT installed on minas.** `AGENTS.md` records that the host
> carries python3 stdlib only — and stdlib includes the `sqlite3` module. Open the
> database READ-ONLY: a validation step must never be the thing that creates a
> `-wal` beside the file you are about to copy.

Run a small python script on minas that opens both the copy and the original with
`sqlite3.connect("file:<path>?mode=ro", uri=True)`, and prints for each:

- `PRAGMA integrity_check` — must be `ok`
- `SELECT count(*) FROM sqlite_master` — must be equal between the two

Observed on the real run (2026-08-16):

```
copy      integrity=ok  sqlite_master=16  tables=6
original  integrity=ok  sqlite_master=16  tables=6
```

### S2 — deploy A, final sync, deploy B

As I2–I4 with `manifests/shelfmark.yaml`, removing its ratchet entry.

### S3 — ⛔ the backup declaration also moves, and it is MINAS' config

`backup-root-data.nix` declares the dump target:

```
"/home/edgar/git/docker/books/shelfmark/config/users.db|" \
```

Change it to `/usr/local/etc/shelfmark/config/users.db`, and remove the third
ratchet entry.

⚠️ **This requires rebuilding MINAS**, after pelargir. The first draft said "expect
pelargir moves, others do not" — wrong; this is a minas closure change.

⚠️ **Order matters.** Rebuild minas only *after* the pelargir path switch has
succeeded. Doing it first points the backup at the new copy while the live app is
still writing the old database.

### S4 — verify the NEW artifact by its exact name

The dump filename is derived from the source path (`s|/|_|g`), so it changes:

```
before:  /storage2/backup/dumps/_home_edgar_git_docker_books_shelfmark_config_users.db
after:   /storage2/backup/dumps/_usr_local_etc_shelfmark_config_users.db
```

```sh
sudo systemctl start backup-root-data
sudo ls -la /storage2/backup/dumps/_usr_local_etc_shelfmark_config_users.db
```

⛔ Do **not** verify with `ls | grep -i shelf`. The old artifact stays in that
directory and nothing prunes it, so a loose grep matches the stale file and reports
a false pass. Check the exact new name and its timestamp, then delete the old one.

The dump directory is `/storage2/backup/dumps`. An earlier draft said
`/storage2/backup-dumps`, which does not exist.

---

## What the 2026-08-16 run actually proved, and one trap it exposed

Verification after the fact, because "the Pod is Running" is not evidence:

| check | result |
|---|---|
| live Deployment `hostPath` | new path on both workloads (read from the running object) |
| `users.db` content hash, old vs new | **identical** |
| whole-tree itemised rsync, old vs new | no differences beyond SQLite sidecars |
| backup run | `Result=success`, new artifact `integrity=ok`, 6 tables |
| external HTTP | `immich.saldivar.io` 200, `books.saldivar.io` 200 |
| open handles on the old trees | none |

**The strongest evidence was behavioural, not declarative.** After its restart,
shelfmark rewrote `settings.json` and `plugins/downloads.json` **in the new tree**
— byte-identical content, fresh mtime `17:23`. The old tree's last application
write is `2026-08-14`. So the app is demonstrably reading and writing the new
location, proven by what it did rather than by reading the spec back.

### ⚠️ The trap: a read-only SQLite open is not side-effect-free

After the cutover, the old tree showed `users.db-wal` and `users.db-shm` with
timestamps *later than the copy*. Nothing was writing there — those files were
created by **the validation step in this runbook**, opening the original
`mode=ro`. Opening a WAL-mode database read-only still touches the shared-memory
index.

Harmless here: `users.db` itself is unchanged since June, the hashes match, and
`-wal`/`-shm` are transient. But the consequence is worth stating, because it
inverts the usual advice:

⛔ **Validate the COPY. Only touch the original if you accept that you are
modifying the source you may still need to roll back to** — and never re-run a
`--delete` rsync afterwards expecting the trees to be identical, because they no
longer are.

## Rollback — not simply "revert the commit"

Before either deploy B: the old tree is untouched and reverting is clean.

**After deploy B, once the workload has accepted writes, the old tree is stale.**
Reverting returns shelfmark to a `users.db` missing everything since the cutover.
A real rollback is: deploy `replicas: 0`, reverse-sync the new tree back over the
old, then deploy the old path with `replicas: 1`.

Both Deployments use `strategy: Recreate` for single-writer hostPath state, so a
brief outage is expected and two writers should never coexist if the staging above
is followed.

## Only afterwards: remove the old trees

Separate session, after at least one full backup cycle:

```sh
sudo rm -rf /home/edgar/git/docker/immich/config
sudo rm -rf /home/edgar/git/docker/books/shelfmark/config
```

## When the ratchet reaches zero

Two bindings remain after this — the traefik file-provider pair, which move
together. When `checks/external-checkout-dependency.nix` has an empty `expected`,
delete that check and `/home/edgar/git/docker` in the same commit.
