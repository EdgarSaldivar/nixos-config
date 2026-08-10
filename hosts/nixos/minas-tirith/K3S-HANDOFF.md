# k3s migration — HANDOFF

**Read this first.** Updated 2026-08-08 after `jellyfin`, `plex` and `readmeabook`
landed, so a fresh session can continue without re-deriving anything. Everything
referenced here is committed.

---

## Where things stand

**Phase 0, Phase 1 and Phase 3 are COMPLETE: 35 of 35 migrated. Docker is at ZERO containers.**

| | |
|---|---|
| on k3s | `audiobookshelf`, `komga`, `palworld`; the **`media` wave (10)**: `tautulli`, `overseerr`, `prowlarr`, `sonarr`, `radarr`, `lidarr`, `animearr`, `maintainerr`, `wrapperr`, `shelfmark`; **tier A (2)**: `kavita`, `calibre`; `flaresolverr`; **`jellyfin`** (2026-08-07, the first StatefulSet — see below); **`plex`** (2026-08-08); **`readmeabook`** (2026-08-08, the first database migration); **`media-tracearr-1`** (2026-08-08, first sops Secret); **`deluge-books`** (2026-08-09, first workload DESIGNED not ported); **`deluge-vpn`**; and the **`gluetun`/`qbittorrent-books`/`flaresolverr-books` netns trio** (one Pod) |
| on docker | **0** containers — `traefik` migrated 2026-08-10, the last one |
| cluster | 2 nodes Ready, Secret encryption Enabled, CoreDNS 2 replicas |

Health check for a new session:

```sh
ssh pelargir 'sudo k3s kubectl get pods -n media; sudo k3s kubectl get pods -n books'
ssh minas 'sudo docker ps -q | wc -l'               # expect 1
```

> ⚠️ KEEP THIS NUMBER TRUE. It read **17** for a while when it was already 15 —
> `flaresolverr` had migrated and was never subtracted — and a health check whose expected
> value is wrong teaches you to ignore it. The **1** as of 2026-08-09 is `traefik`.
> (immich, immich-postgres14 and immich-redis all migrated 2026-08-09.) (`nextcloud` and `nextcloud-db` migrated
> 2026-08-09; `nextcloud-redis` was RETIRED, not migrated — nothing used it.)
> (`deluge-books` migrated 2026-08-09 — subtracted the same day, per the warning above.
> Then `deluge-vpn` and the `gluetun`/`qbittorrent-books`/`flaresolverr-books` netns trio
> migrated, taking 11 → **7**; the stale 11 was caught on 2026-08-09 while building the
> nextcloud rollback artifacts. Verified against the exited list, not assumed.)

Verify ingress against `K3S-BASELINE-MEDIA.md` — **not** against 200. Six of these
hostnames return 303/307/302/401 when perfectly healthy, and `maintainerr.saldivar.io`
returning **200 instead of 401** means the `basic-auth@file` middleware was dropped.

> ⚠️ `ssh minas`, **not** `minas-tirith`. `docker` needs `sudo` there and without it
> reports **zero** containers rather than failing.

---

## ✅ nextcloud — MIGRATED 2026-08-09. Docker 7 → 4.

The largest-state workload on the fleet (1.5 TB) is on k3s and serving.
`drive.saldivar.io` → **302**, matching baseline; `/login` → 200. Both Pods 1/1 on
minas-tirith, zero restarts.

**Acceptance was byte-level, not row counts.** The db Pod adopted the EXISTING cluster —
`verify-pgdata` reported system identifier `7147093535374221351`, `pg_is_in_recovery` false
— and identity matched the pre-cutover baseline exactly: 4 users with display names, 5
storage id strings, per-storage counts AND bytes, 124415 filecache rows. Then all **20
known files re-hashed: matched=20, drifted=0, missing=0**.

Rollback artifacts, retained: `storage2@nextcloud-cutover-20260809T234032Z` (data + a
verified shut-down PGDATA) and `/storage2/backup/nextcloud-cutover-20260809T234032Z/`.
⚠️ The snapshot pins blocks as the 1.5 TB churns — destroy it once accepted.

### ⛔ THE TRAP THAT COST DOWNTIME: kubelet's httpGet sends the POD IP as `Host`

nextcloud answers **400** for any host outside `NEXTCLOUD_TRUSTED_DOMAINS`
(`drive.saldivar.io`), so the `httpGet /status.php` **startupProbe** failed 17 times.

The damage is what that does downstream: **Kubernetes suppresses readiness and liveness
until the startupProbe succeeds**, so the new DB-backed readiness probe NEVER RAN. It
presents as "readiness is broken" while readiness was never evaluated — and sends you to
debug the wrong probe. MEASURED in-Pod: `Host: 127.0.0.1` → 200, `Host: <podIP>` → **400**,
`Host: drive.saldivar.io` → 200. Fixed with an explicit `httpHeaders` Host entry.

⚠️ This defect had been sitting in the staged manifest all along and had **never executed**,
because the Deployment was at `replicas: 0`. Staging at zero hides probe defects completely.
Verify a probe the way the PROBE will call it, not the way `curl` does.

### ⛔ `docker compose up -d nextcloud` is a VERSION UPGRADE, not a rollback

The compose entry says `nextcloud:latest`; the stopped container is **28.0.14.1**. A compose
recreate would pull a newer major version and run its migrations against the live database.
**Rollback is `docker start <name>`.** All three services now carry `profiles: ["migrated"]`
(verified absent from `docker compose config --services`), but that is a guard against
accident, not a fence — `docker start` still bypasses compose.

## The original scoping notes, kept — UN-DEFERRED 2026-08-09, blocker ADDRESSED

The owner directed this to proceed after the backup gap was re-stated. What changed is not
the decision but the **protection**, and it is worth understanding because three earlier
scopes were rejected precisely for lacking it.

### ⛔ SUPERSEDED / DO NOT USE — the first 08:40 rollback claim

The following account is retained as rejected reasoning history. It is **OBSOLETE**:
`storage2@nextcloud-precutover-20260809T084029Z` was taken while the containers were
running, does not cover the app tree, and is not coherent with the separately-created
dump. It must not be used for rollback. Only the corrected 09:30 quiesced-artifact
account below is actionable.

`/storage2/nextcloud/data` is 1.5 TB and is NOT a backup source — that has not changed.
But `/storage2` is **ZFS**, so:

```
storage2@nextcloud-precutover-20260809T084029Z      <- created 2026-08-09, cost 0B
```

This was originally claimed to make the cutover fully reversible. **That claim was
wrong.** A copy-on-write snapshot is instant and initially free, but this one captured a
running database and omitted the app tree. ⚠️ It was never a backup either: it is on the
same pool and provides no protection against disk failure or pool loss.

⛔ Do not let this snapshot linger: it pins every block the data overwrites, so it grows
with churn on a 1.5 TB dataset. Destroy it once the migration is accepted.

### ⛔ SUPERSEDED / DO NOT USE ALONE — the separately timed 08:41 dump

⚠️ The db lives at `/storage2/nextcloud/db` and is therefore ALSO outside the backup
sources — that was not previously called out. It is only 170 MB, so unlike the data it can
have a proper one, and now does:

```
/storage2/nextcloud-precutover-20260809T084122Z/nextcloud-db.dump   (13M, pg_dump -Fc)
```

The dump was restore-tested into an isolated `postgres:14.5`: `pg_restore` exit 0, and
row counts compared against live — `oc_users` 4/4, `oc_filecache` **124415/124415**,
`oc_storages` 5/5. That proves the dump is readable; it does **not** make it coherent with
the earlier running-filesystem snapshot. Retain this as history, but do not pair these
08:40/08:41 artifacts for rollback. Use the corrected quiesced set below.

### ⛔ THE SNAPSHOT IS NOT SUFFICIENT ON ITS OWN — found in review 2026-08-09

Two CRITICALs, both about protection rather than the manifest:

1. ⛔ **The 2026-08-09 08:40 snapshot is NOT cutover-consistent, and does NOT cover
   everything.** It was taken while all three containers were RUNNING, so it is a torn
   moment; the database dump was taken separately and does not correspond to it. And
   VERIFIED: `/usr/local/lib/docker-nextcloud` (the 606 MB app tree, including
   `config.php`) sits on `/dev/mapper/cr_root` at `/` — a DIFFERENT FILESYSTEM from
   `storage2`. **The snapshot never covered it.**

   ✅ Before cutover: enable maintenance mode, stop every writer, stop PostgreSQL cleanly,
   prove no open handles, THEN take a fresh snapshot AND a stopped archive of the app tree
   at the same moment. Those three artifacts must be coherent with each other.

   ⛔ Rollback must NOT be `zfs rollback storage2@…` — that is the whole 3.1 TB parent
   dataset and repeats the unrelated-tree blast radius that got an earlier scope rejected.
   Use a clone or selective restore.

   ### ✅ DONE 2026-08-09T09:30:59Z — the artifacts exist. See `NEXTCLOUD-ROLLBACK-RUNBOOK.md`

   Built in one quiesced window, each step gated: maintenance mode on → app and redis
   `Exited (0)` → `pg_dump -Fc` while postgres still ran with no writers left → postgres
   stopped in **0.255 s** → **`pg_controldata: shut down`** (not `in production`) →
   `postmaster.pid` absent and **0** open handles → snapshot, then the app-tree tar.

   | | |
   |---|---|
   | snapshot | `storage2@nextcloud-quiesced-20260809T093059Z` — data **and** PGDATA |
   | app tree | `/storage2/backup/nextcloud-precutover-20260809T093059Z/app-tree.tar.gz` (206 MB, 28245 entries, has `config.php`) |
   | dump | same dir, `nextcloud-db.dump`, **restore-tested with ZERO diagnostic lines** |
   | baseline | same dir, `identity-baseline.txt` — identities, not just counts |

   ✅ **Coherence is verified by the whole capture chain, not by `pg_controldata`
   alone**: writers were absent, PostgreSQL was stopped cleanly, one atomic ZFS snapshot
   captured data and PGDATA together, and the snapshot's control data matches `shut down`
   at checkpoint `4/275F04A0`, identifier `7147093535374221351`. `pg_controldata` alone
   cannot prove an arbitrary non-atomic copy untorn. The restore-test matched all 4 user
   ids/displaynames, all 5 storage id strings, per-storage counts **and** bytes, 103 tables,
   and 124415 filecache rows.

   ⚠️ Total window was **under four minutes**, and docker was restarted after
   (`drive.saldivar.io` → 302, matching baseline). So these are a point-in-time from
   09:30:59Z, **not** the cutover moment — **re-take them at cutover**. It is cheap.

   ⚠️ `/storage2/nextcloud` is a DIRECTORY in the `storage2` root dataset, not a dataset of
   its own — which is precisely why `zfs rollback` is forbidden here. Selective restore
   reads from `/storage2/.zfs/snapshot/<snap>/nextcloud/`; no clone needed.

   ⛔ The snapshot costs `0B` now and grows with churn. The older torn
   `…precutover-20260809T084029Z` is superseded and can be destroyed.

2. ⛔ **`strategy: Recreate` is NOT a cross-runtime interlock.** It prevents overlapping
   Kubernetes revisions only; it cannot stop a retained Docker container from reopening the
   same PGDATA. ✅ All three containers were set `restart=no` on 2026-08-09 (still running,
   undisturbed). At cutover: stop app BEFORE database, and prove process and open-file
   absence before starting either Pod. On rollback: scale BOTH replicas to zero
   declaratively and verify termination BEFORE starting Docker.

### ✅ Review findings to close before replicas 1 — ALL FOUR ARE CLOSED (2026-08-09)

Kept with their original reasoning, because each explains a trap that will recur.

- ✅ ~~No `nextcloud-db` Service exists yet.~~ **DONE** (`ada6006`). Both Services are staged
  and live: `nextcloud` at `10.43.20.53`, `nextcloud-db` at `10.43.178.130`. Inert — a
  Service with no ready endpoints routes nothing.
- ✅ ~~`/status.php` returns 200 during maintenance mode and proves nothing database-backed;
  `pg_isready` proves the server accepts connections, not that the named database and user
  authenticate.~~ **DONE** (`fa885c2`, deployed). App readiness is now an exec requiring
  `installed:true` **and** `maintenance:false`, then a real PDO `SELECT 1` against the
  database in the **mounted `config.php`** — not the `POSTGRES_*` env, which an installed
  Nextcloud ignores because those only feed the image's first-run installer. DB readiness is
  `psql … -c "SELECT 1"` asserting the result is exactly `1`. MEASURED live: **0.140 s** and
  **0.092 s**. `startupProbe` stays `pg_isready` / `httpGet` — right tool for "has it booted".
- ✅ ~~`type: Directory` does not prove VALID state.~~ **DONE** (`fa885c2`, deployed). A
  read-only `verify-pgdata` initContainer on the same pinned digest requires `PG_VERSION=14`,
  non-empty `global/pg_control`, `base/`, **no `postmaster.pid`**, and `pg_controldata`
  reporting a shut-down cluster. VERIFIED against all three inputs: clean snapshot PGDATA
  → exit 0, empty directory → exit 1, live running PGDATA → exit 1.
  ⛔ It deliberately does **not** assert the system identifier — a supported logical restore
  creates a new one, so an equality check would refuse a correctly restored database.
  ⚠️ It also refuses to start after an **unclean** shutdown; that is a deliberate trade while
  docker is retained, and the manifest carries the reasoning and the recovery procedure.
- ✅ ~~Counts alone are NOT an acceptance baseline.~~ **DONE.** `identity-baseline.txt` holds
  user and storage IDENTITIES plus per-storage counts/bytes, and `known-file-identities.txt`
  adds sha256 + fileid for 21 known files, computed from the snapshot. ⛔ Still never use
  `files:scan --all` as validation — it mutates the structure being compared.

**What remains before `replicas: 1` is a cutover decision, not a code gap.** Re-take the
quiesced artifacts at the cutover moment, then follow `NEXTCLOUD-ROLLBACK-RUNBOOK.md`.
⚠️ `traefik-routes.nix` has not changed since `50d78a8`, so **minas needs no rebuild** for
any of this; pelargir alone delivers it. Confirm that again at cutover rather than assuming.
- ⚠️ Dropping `nextcloud-redis` IS supported by the evidence, but re-check the effective
  settings from inside the Pod before acceptance.
- ✅ The duplicate data mount and omitting the nginx config were both confirmed correct.

### Measured facts for the migration### Measured facts for the migration

| | |
|---|---|
| app | `nextcloud:28-apache`, version **28.0.14.1**, installed, not in maintenance, `needsDbUpgrade: false` |
| db | `postgres:14.5`, database **`nextcloud-db`** (170 MB) |
| redis | `redis:6.2-alpine`, `/usr/local/lib/docker-nextcloud-redis` (93 bytes — effectively empty) |
| app tree | `/usr/local/lib/docker-nextcloud` -> `/var/www/html` (606M, IS in the backup sources) |
| data | `/storage2/nextcloud/data` -> **mounted TWICE**, at `/var/www/html/data` AND `/var/www/data` |
| ingress | `drive.saldivar.io`; `OVERWRITEPROTOCOL=https`, `OVERWRITECLIURL`, `NEXTCLOUD_TRUSTED_DOMAINS` |
| secrets | `POSTGRES_USER` / `POSTGRES_PASSWORD` are plaintext env -> must go to sops |

### ⛔ Two traps found before writing anything

1. **`nextcloud-redis` is UNHEALTHY and has been for days — 7161 consecutive failures — but
   REDIS IS FINE.** Its healthcheck runs `redis-cli -a <password> ping` against a server
   with no `requirepass`, so it fails with
   `AUTH failed: ERR AUTH <password> called without any password`. ⛔ Translating that
   healthcheck into a Pod probe unchanged gives a Pod that NEVER becomes Ready. Fix the
   check, do not port it.
2. **A `.my_custom_proxy_settings.conf` is mounted to `/etc/nginx/conf.d/` on an APACHE
   image.** Almost certainly dead config from a previous nginx-based setup. Do not port it
   without establishing what reads it — nothing in an apache image does.

---

## ✅ immich — MIGRATED 2026-08-09. Docker 4 → 1. Only traefik remains.

`immich.saldivar.io` → **200**, `/api/server/ping` → `{"res":"pong"}`, version 1.142.0.
All three Pods 1/1, zero restarts.

Acceptance matched the quiesced baseline **exactly**, including the data most at risk:
`asset` 4931, `person` 124, `album` 11, `users` 1, and **`face_search` 2392 /
`smart_search` 3706** — the pgvecto-rs embeddings survived, with `vectors 0.2.0` loaded.
`verify-pgdata` reported identifier `7505259935988518951`, the pre-cutover value, so the Pod
adopted the existing cluster rather than initialising over it.

Artifacts: `storage@immich-cutover-20260810T021615Z` plus
`/storage2/backup/immich-cutover-20260810T021615Z/{immich-db.dump,identity-baseline.txt,config-tree.tar.gz}`.
⛔ `config-tree.tar.gz` exists because the 766 MB config tree is on `cr_root`, a DIFFERENT
filesystem the `storage` snapshot cannot reach — the same gap that made nextcloud's first
snapshot an invalid rollback.

### ⛔ I MADE THE TWO-HOST MISTAKE AGAIN, and it caused a 404

Immich's traefik route was NEW in the staging commit. I rebuilt **pelargir only**, so the
Pods came up healthy — `hostPort 3001` answered 200 — while `immich.saldivar.io` served
**404** because `k8s-immich.yml` had never been installed. `traefik-routes.nix` is delivered
by **minas**. Rebuilding minas fixed it immediately.

⚠️ What made this easy to walk into: this file said "minas needs no rebuild" — which was
true **for nextcloud**, whose route already existed from an earlier commit. It is false for
any service whose route is introduced in the same change. ⛔ The rule is not "minas rarely
needs a rebuild"; it is **check whether `traefik-routes.nix` changed since minas' checkout**,
every time.

### ⛔ Profile compose services by their KEY, not their container_name

`immich/docker-compose.yml` names its services `immich`, `redis`, `postgres14` while their
`container_name`s are `immich`, `immich-redis`, `immich-postgres14`. Profiling by
container_name matched only one of three, and `docker compose config --services` still listed
`postgres14` and `redis` — meaning a bare `docker compose up -d` would have started a second
Postgres on the live PGDATA. The gate that matters is that the **default service list is
empty**, not that some names disappeared from it.

## The original survey, kept — the blocker it describes is now closed

The last stateful workload. 3 of the 4 remaining docker containers. Everything below is
measured, not inferred.

| | |
|---|---|
| app | `ghcr.io/imagegenius/immich:1.142.0-cuda` |
| db | `tensorchord/pgvecto-rs:pg14-v0.2.0` — **not plain postgres**, it carries the `vectors` extension |
| redis | `redis`, **no mounts at all** — stateless, recreate freely |
| photos | `/storage/immich-data/photos` — **44 GB** |
| libraries | `/storage/immich-data/libraries` — 512 bytes, effectively empty |
| pgdata | `/storage/immich-data/pgdata` — 168 MB (database 170 MB) |
| config | `/home/edgar/git/docker/immich/config` on `cr_root` — a DIFFERENT filesystem, and IS a backup source |
| ingress | `immich.saldivar.io` → **200** baseline. `photos.saldivar.io` does not resolve |
| identity | asset **4931**, person **124**, album **11**, users **1** |

### ✅ THE BLOCKER IS CLOSED — the photos have a real backup as of 2026-08-09

`/storage/immich-data` is now a backup source. First run copied **43 G, 15790/15790 files**,
`pgdata` correctly excluded, service `success`, and the post-rsync snapshot
(`storage2/backup@daily-20260810-003333`) means it is history rather than a mutable mirror.
So immich no longer carries the gap that got three nextcloud scopes rejected — it can be
migrated on its own merits.

⛔ `pgdata` is excluded on purpose: it is a live cluster, and rsync of a running Postgres
produces something that looks like a backup and is not one. The database is covered by the
dump loop (`immich-postgres14.sql.gz`, verified to contain the pgvecto-rs extension and both
embedding tables at exact live row counts).

#### The original reasoning, kept — why 44 GB was fixable and 1.5 TB was not

`/storage/immich-data` is on the `storage` pool, which is **NOT in the filesystem backup**
(that covers `/etc /home /usr/local /opt /srv` plus container trees; the only `/storage`
path rescued is calibre's `metadata.db`). So 44 GB of irreplaceable photos have no backup
today — the same gap that got three nextcloud scopes rejected.

⚠️ **But 44 GB is not 1.5 TB.** `storage2` has ~4 TB free, so unlike nextcloud this data can
have a *real* backup rather than only a same-pool snapshot. Do that first and the migration
stops being fragile. That is the recommended order.

`/storage` IS ZFS (51.3 T used, 7.91 T free), so a quiesced snapshot works the same way —
and ⛔ `immich-data` is a DIRECTORY in the `storage` ROOT dataset (only `storage/pincollector*`
are child datasets), so `zfs rollback` is forbidden here for the same blast-radius reason.
Selective restore reads `/storage/.zfs/snapshot/<snap>/immich-data/`.

### ✅ pgvecto-rs DOES dump correctly — verified, not assumed

This was the real risk: `pg_dumpall` silently destroyed TimescaleDB's hypertables for
`media-tracearr-1` and still exited 0, so the vector extension needed the same scrutiny.
It passes. The existing `/storage2/backup/dumps/immich-postgres14.sql.gz` (34 MB) contains
`CREATE EXTENSION IF NOT EXISTS vectors WITH SCHEMA vectors` and both COPY blocks, and the
row counts match the live database **exactly**:

| table | live | in dump |
|---|---|---|
| `face_search` | 2392 | **2392** |
| `smart_search` | 3706 | **3706** |

⚠️ Restoring it requires the **same pgvecto-rs image**, not stock postgres — the four
`vector(512)` columns (`face_search`, `smart_search`, `face_index`, `clip_index`) need the
extension present.

### ⚠️ The `-cuda` image has NO GPU attached

`DeviceRequests` is `null` and `nvidia-smi` is absent inside the container, so immich runs
its CUDA build entirely on CPU today. Two consequences: migrating it needs **no** third GPU
(allocatable is 2, and jellyfin + plex hold both), and if hardware ML is ever wanted, that
is a separate change — see the plex `LD_LIBRARY_PATH` finding before assuming it is simple.

### ⛔ n_live_tup LIED, and it would have produced a false finding

`pg_stat_user_tables` reported `face_search=0` and `smart_search=0`; real `count(*)` returned
**2392** and **3706**. Reported as-is that reads "immich's ML has produced nothing" — and
combined with the missing GPU it would have been a very convincing wrong conclusion.
`n_live_tup` is an estimate maintained by autovacuum. Never use it for acceptance.

## ⛔ DEFERRED by owner decision, 2026-08-07: `nextcloud` and `immich`

Both are **database + irreplaceable user data** migrations, and both are deferred until
everything else has moved. Do not pick them up as "the next one".

Three successive scopes for nextcloud were written and **all three were rejected by
cross-review**, each time for genuine data-loss paths rather than polish — a namespace-blind
`postmaster.pid` offering no cross-runtime interlock, a `zfs rollback` whose real blast
radius included unrelated trees, `psql` returning success after SQL errors, auto-deploy
reapplication resurrecting deleted Deployments. The reviews were right every time.

The blocker is not any single defect. It is that nextcloud is the first migration with a
real database, a real credential, AND **1.5 TB of user files that no backup covers**
(`/storage2/nextcloud/data`; the filesystem backup takes `/etc /home /usr/local /opt /srv`
plus the container-storage trees, not `/storage2`).

**Revisit when there is a real backup of that data.** That removes the constraint that
makes every plan fragile, because rollback stops depending on getting one cutover perfect.
Until then nextcloud runs fine on docker and blocks nothing — Phase 6 moves ingress
regardless.

The three rejected scopes and their findings are worth re-reading before attempt four;
the fourth-round blockers were the init gate releasing early AND hanging forever, stale
auto-deploy files surviving a `manifests.nix` edit, and `docker rm` converting a corruption
risk into a split-brain risk.

---

## PREREQUISITE landed for tier C: unsafe sysctls

`minas-tirith/k3s.nix` now allows `net.ipv6.conf.all.disable_ipv6` and
`net.ipv4.conf.all.src_valid_mark`. Three services need it —
`flaresolverr`, `deluge-vpn`, `deluge-books` — and `src_valid_mark` is a **WireGuard
requirement**, so the whole VPN pair is blocked without it.

⚠️ Applying it **restarts the k3s agent**. Plan it — but it is milder than this file used
to imply.

✅ **APPLIED 2026-08-08 14:40** and measured: all 22 pods on minas were Running again
**within 10 seconds**, and every ingress hostname answered its baseline immediately.
Containers are re-adopted rather than rebuilt.

⚠️ **A rebuild is NOT enough — the unit changes but the running process does not.** The
flag sat in `k3s.service` from an earlier commit while the live agent had been started
before it, so the prerequisite looked satisfied and was not. `systemctl restart k3s` is
the step that applies it.

⚠️ And do NOT verify it by grepping the process command line. NixOS puts the flag on a
CONTINUATION LINE of `ExecStart`, so `grep '^ExecStart'` shows only `k3s agent` and the
live `/proc/<pid>/cmdline` reads bare too — both look like the flag is missing when it is
not. The only trustworthy check is a real Pod:

```sh
# a Pod requesting the sysctls; if it reaches Running, they are live
securityContext.sysctls: [ {name: net.ipv4.conf.all.src_valid_mark, value: "1"} ]
```

Verified that way: the values read back as `1` and `1` from inside the Pod. Note the
PodSecurity `baseline` warning about forbidden sysctls is expected — the namespaces are
audit/warn with no `enforce`, exactly as for calibre's seccomp field.

---

## THE NEXT TASK

No group cutover is pending. What remains is individually-scoped work:

1. ~~`readmeabook`~~ **DONE 2026-08-08** — the first database migration. What it
   established, and what `media-tracearr-1` should copy:
   - **Re-scan the application DATABASE at migration time.** This file said readmeabook
     "has no edges"; its `configuration` table held three. See the correction below.
   - **A restore-TESTED dump before touching anything.** `pg_dumpall` from the LIVE
     container, restored into an isolated `postgres:16`, every error captured rather than
     trusting the exit code, and counts compared. That is the artifact that makes the
     rest safe.
   - **`pg_controldata` as a gate.** After `docker stop` the cluster read
     `Database cluster state: in production`, NOT "shut down" — supervisord does not stop
     Postgres gracefully. The copy is crash-consistent and Postgres replays WAL on start
     (it did, `pg_is_in_recovery: f`), but plan for two ways back rather than a clean copy.
   - **`rsync -aHAX --numeric-ids`**, because PGDATA is uid **102** and a hostPath does
     not remap uids. No `runAsUser`, no `fsGroup`.
   - **Leave the source volumes in place.** `rmab-pgdata`/`rmab-redis` are untouched at
     checkpoint 1/59101D50 and are the primary rollback.
2. ~~`media-tracearr-1`~~ **DONE 2026-08-08.** Two things it added to the playbook:
   - **`pg_dumpall` IS NOT A BACKUP for TimescaleDB.** It drops every hypertable and
     continuous aggregate and still exits 0. `system.nix`'s declared-dump entry grew a
     MODE field; tracearr takes `fc` (per-database `pg_dump -Fc`) and the loop DELETES
     the misleading `.sql.gz` the discovery loop also writes. Restore procedure is in
     `RESTORE-RUNBOOK.md` and was verified end to end.
   - **Secrets go to sops, never inline.** `JWT_SECRET`/`COOKIE_SECRET` were plaintext in
     compose; they now live in `secrets/cluster-apps.yaml`, render to tmpfs and are
     applied by `k3s-apply-secrets`. ⚠️ That unit is `RemainAfterExit=true`, so it
     silently never re-applied a changed Secret until `restartTriggers` were added —
     a value-only rotation STILL needs a manual `systemctl restart k3s-apply-secrets`.
3. ~~`plex`~~ **DONE 2026-08-08.** Only two bridges remain — `deluge-books` and
   `deluge-vpn` — so the no-inert-window rule now applies only to those.
4. ~~`deluge-books`~~ **DONE 2026-08-09** — see the cutover section below. It was
   REDESIGNED rather than ported (gluetun sidecar, unprivileged), and the design plus its
   runbook are in `K3S-VPN-STACK-DESIGN.md`.
   **`deluge-vpn` is next and follows the same design** — same gluetun pattern, its own
   tunnel and forwarded port. It needs its own state baseline and copy; do NOT assume
   deluge-books' evidence covers it.
   Then the `gluetun` + `qbittorrent-books` + `flaresolverr-books` netns trio, which move
   together because the latter two share gluetun's network namespace.

   ✅ **PREREQUISITE IS APPLIED** (2026-08-08) — see the sysctls section above.

   ✅ **`privileged: true` IS NOT REQUIRED, and this was tested rather than assumed** —
   the Phase 4 instruction not to translate it mechanically. The docker container runs
   with ALL capabilities (`CapEff: 000001ffffffffff`). A probe Pod on minas showed that
   **`NET_ADMIN` + an explicit `/dev/net/tun`** does everything `init.sh` needs:

   | with `NET_ADMIN` only | with `NET_ADMIN` + `/dev/net/tun` hostPath |
   |---|---|
   | `iptables -P OUTPUT DROP` **OK** | same |
   | `src_valid_mark` reads `1` | same |
   | `/dev/net/tun` **ABSENT** → tun create FAILED | device present, tun **created and brought up** |

   So the missing piece was never a capability — it was the device, which `privileged`
   supplies implicitly. Mount it explicitly:

   ```yaml
   volumes:      [ { name: tun, hostPath: { path: /dev/net/tun, type: CharDevice } } ]
   volumeMounts: [ { name: tun, mountPath: /dev/net/tun } ]
   securityContext: { capabilities: { add: ["NET_ADMIN"] } }
   ```

   ⛔ **THE DESIGN WAS REJECTED BY CROSS-REVIEW (2026-08-08).** Nothing was built. The
   findings below ARE the design for attempt two — read them before writing a manifest.

   **BLOCKER 1 — moving the credentials to a Secret does NOT get them off disk, and this
   is a problem TODAY.** binhex's startup writes `VPN_USER`/`VPN_PASS` into
   `/config/openvpn/credentials.conf`, and `/config` is the hostPath
   `/usr/local/etc/deluge-books`. Verified: the file exists, mode 775, and `/usr/local/etc`
   is an **rsync source of the nightly backup** — so the PIA credentials are in every
   backup and every ZFS snapshot right now. A `secretKeyRef` fixes only the repo exposure.
   The credential file needs memory-backed storage, and the credentials need ROTATING
   (already on the rotate list) because they are long since exposed.

   **BLOCKER 2 — the kill-switch has a Kubernetes-shaped bypass the planned proof misses.**
   binhex dynamically trusts its detected container-interface CIDR, which in a Pod is the
   POD SUBNET. Another Pod on the same node, or a node-side relay, can therefore carry
   traffic outside `tun0` — and probing public `1.1.1.1` still reports success, so the
   obvious test passes while the leak exists. Test adversarially FROM A SAME-NODE POD and
   against the node gateway, not just outbound to the internet.

   Serious, all real:
   - **IPv6 is untested.** The image strips OpenVPN's IPv6 config and only *warns* if
     `ip6tables` is missing. Pods have link-local IPv6 today, but Phase 4 explicitly
     requires IPv6 and cluster-DNS bypass testing. Attempt IPv6 TCP/UDP/DNS egress with
     the tunnel down.
   - **`ip link set tun0 down` proves less than it looks** — OpenVPN stays alive and can
     recreate the interface. Kill the process AND blackhole its endpoints, hold the
     failure state, and attempt NEW flows continuously.
   - **The public route was omitted.** `btbooks.saldivar.io` exists only via the docker
     label; stopping docker deletes that router. It needs a `traefik-routes.nix` entry to
     `deluge-books.media.svc.cluster.local:8112`.
   - **Pinning the ClusterIP does not make the takeover continuous**, and the manual
     `deluge-books-docker` EndpointSlice must be deleted BY HAND or kube-proxy advertises
     a stopped docker endpoint beside the Pod.
   - **Digest-pin the image.** `2.1.1-8-03` is a tag; the inventory records
     `sha256:737ef923e400bf6e00595ce6c8fd419002985617e5304a15e66808b1c893b2de`. This is
     the worst component on the fleet to allow drift in — the tag republishing would
     silently change the firewall implementation.
   - **No state rollback gate.** `/config` holds Deluge's queue and session state and is
     shared with the docker rollback path, so the Pod can rewrite it before acceptance.
     Take a validated stopped copy and record torrent/queue identity first. Declare every
     hostPath `type: Directory` (and `/etc/localtime` as `File`) — without types a typo
     presents as a fresh, data-less Deluge.
   - **`capabilities.add` is not `capabilities.only`.** Kubernetes' default set remains.
     Record the resulting `CapEff` and test the exact set; `drop: [ALL]` may break the
     image's root init. And `automountServiceAccountToken: false` was omitted.

   ⚠️ **Sequencing matters more here than anywhere else.** Adding the selector Service and
   raising replicas together exposes the Pod to prowlarr IMMEDIATELY. Gate readiness, do
   the leak acceptance by hand, and only then let the endpoint become Ready.
5. **`nextcloud`+db+redis and `immich`+postgres14+redis** — self-contained on their own
   networks, but real database migrations needing D6's dump/restore. Note their postgres
   dumps are what the backup's docker discovery loop currently finds; when they leave
   docker that loop matches nothing, which is why it now carries `|| true`.
6. **The `gluetun` / `qbittorrent-books` / `flaresolverr-books` netns trio** — they share
   a network namespace, so they become ONE multi-container Pod.
7. **`flaresolverr`**, then **`traefik`** last (Phase 6 — that is what finally moves
   ingress off docker).

### What tier A (`kavita`, `calibre`) taught

- **Not everything needs a route.** `calibre` has NO traefik labels and no public
  hostname — it is reached only on host ports 8080/8081/8181, so those hostPorts are its
  sole access path. Inventing an ingress would newly publish a UI whose `PASSWORD` is
  empty. Check for traefik labels before assuming a service wants a route.
- **`security_opt` must survive translation.** `calibre` declares
  `security_opt: seccomp:unconfined`; the Pod needs
  `securityContext.seccompProfile.type: Unconfined`. Dropping it is INVISIBLE today
  (k3s sets no seccomp default) and breaks the day one is set. It logs a PodSecurity
  `baseline` warning — the namespaces are audit/warn with no `enforce`, so it is admitted.
  Do not "fix" that warning by deleting the field.
- **Independent services do not need a wave.** A live scan found no bare-name edges in
  either direction for either service, so they cut over one at a time with per-service
  rollback. Verify that with a scan rather than assuming it.

---

## deluge-books: MIGRATED 2026-08-09 — the first workload DESIGNED rather than ported

Docker 12 → **11**. The first VPN-gated service, and the first built from
`K3S-VPN-STACK-DESIGN.md` instead of translated. Result: 47 torrents, **41 seeding / 6
downloading — the exact pre-cutover distribution — with ZERO rechecking**.

binhex `privileged: true` (every capability) → **gluetun sidecar, unprivileged, five
MEASURED capabilities**; credentials from a hostPath file → **sops → Secret VOLUME**, never
in env or containerd metadata; MAM cookie in argv → sops; bespoke `init.sh` kill-switch →
gluetun's, plus containment closing the local-node relay. Deluge 2.1.1 → 2.2.0.

**The two design bets that paid off:**

- **In-place Service update.** The Service stayed in `docker-bridges.yaml` and was patched
  selectorless → selector-backed. ✅ **Service UID was IDENTICAL before and after**
  (`dc6affa9…`), proving a patch and not a delete-and-recreate. prowlarr never lost its
  download client; no ClusterIP pinning was needed. Moving it between AddOns — which three
  earlier drafts proposed — would have had the later-applying AddOn prune the Service the
  other had just created.
- **Traefik route installed EARLY**, while docker still served the hostname. At
  `priority: 1` it was inert (verified: traefik logged `"deluge-books@docker"`), and when
  docker stopped the router vanished and the file route took over by itself. `btbooks`
  went 200 → 200 with **no timing at all**, and one host activation left the downtime
  window entirely.

### ⛔ What the gates caught that would otherwise have been silent

1. **The MAM session was ALREADY DEAD** before the migration began. `dynamicSeedbox`
   returns `Set-Cookie` with `Max-Age=1296000` — the session expires **15 days after its
   last use** — and the docker setup only called MAM at container start with a hardcoded
   value. The registrar's startup gate blocked Deluge rather than letting it announce from
   an unregistered IP. ⚠️ The registrar now REFRESHES ON A 6-DAY TIMER, not only on IP
   change; an IP-change-only trigger has exactly the docker failure mode.
2. **portsync could not set the listen port.** gluetun had a port forwarded and nothing
   consumed it — the Pod would have been Ready and serving its UI while announcing a stale
   peer port, which presents as "slow torrents", never as a fault. `deluge-console` is
   unusable here twice over: it needs a writable `$HOME/.config` (EACCES on
   `/home/nobody`), and given one it dies non-interactively with
   `'ConsoleUI' object has no attribute 'started_deferred'`. Use the RPC API.

### ⛔ FIVE traps this cutover hit — all cost time, none cost data

1. **`kill 1` and even `kill -9 1` inside a container DO NOTHING.** PID 1 of a namespace is
   signal-protected from *within* that namespace; `kill` reports success and the process
   lives. `restartCount` staying 0 is the tell. To restart ONE container without restarting
   the Pod (which would reconnect gluetun and change the exit IP), stop it **host-side**:
   `k3s crictl stop <id>`.
2. **The Mac and pelargir CANNOT reach public ingress** — `jellyfin` fails identically from
   both, so it is hairpin NAT, not a fault. ⛔ Run ingress checks **from minas**, or a
   GO/NO-GO gate reports a total outage that is not happening.
3. **Traefik's access log has NO hostname field.** Grepping it for `btbooks` matches
   nothing and reads as failure. The router identity is the `"deluge-books@docker"` /
   `"@file"` field — which is also the best evidence of *which* router is winning.
4. **`deluge-vpn` and `deluge-books` use the SAME image.** A compose edit anchored on
   `binhex/arch-delugevpn` lands on the wrong service. Anchor on the TAG
   (`2.1.1-8-03`) and verify the enclosing service name before writing.
5. **`git add -A` swept staged-but-deliberately-uncommitted edits into an unrelated
   commit**, publishing the cutover manifests during a secrets deploy rather than as a
   deliberate step. Outcome was correct; the decision was not made. ⛔ Do not `git add`
   edits you are preparing but not yet publishing.

### Rollback artifacts — RETAINED

- Docker container **kept, stopped, `restart=no`** (id recorded in the cutover evidence
  dir). Rollback is `docker start <id>`, ⛔ never a compose recreate — the compose file may
  have drifted, which is how btbooks broke on 2026-08-07.
- `/usr/local/etc/deluge-books` **untouched**; the Pod uses a separate
  `/usr/local/etc/deluge-books-k3s`.
- ZFS payload snapshot `storage@deluge-books-cutover-20260809T030306Z` retained as a soak
  artifact. ⚠️ It holds space as torrents write — destroy it once satisfied, and note the
  rollback window stays open until then.
- Full evidence (per-infohash snapshots before/after, digests, boot-ids) under
  `~/Development/deluge-books-cutover-evidence/`.

---

## ▶ START HERE: ✅ MIGRATION COMPLETE — 35 of 35. Docker is at ZERO containers.

**`traefik` cut over 2026-08-10T06:24:31Z. Total ingress downtime: ~30 seconds.**
Acceptance was `matched=55 drifted=0 missing=0 errors=0`, exit 0 — all 26 hostnames, all
three certificate identities, the port-80 redirect and every per-SNI fingerprint. All 25
correlated hostnames were served by an `@file` router; zero by any other provider.

### ⛔ THE ONE THING LEFT, AND IT IS A LIVE LANDMINE: durable promotion (phase C)

**The delivered manifest declares `replicas: 0` while the live Deployment runs `1`.** That
is the deliberate cutover state (see `TRAEFIK-CUTOVER-RUNBOOK.md` §1) — but it is not a
resting place. k3s auto-deploy re-applies a manifest when its **checksum changes** *or* when
the **server restarts**, and either event reasserts `0` and takes **all 26 hostnames down**
with nothing in git to explain why.

This is the same trap that already has 15 other services one k3s restart from an outage —
except here the blast radius is the entire public ingress.

```sh
# PHASE C: commit replicas: 1 in manifests/traefik.yaml, rsync, rebuild pelargir, then:
sudo grep -E '^  replicas:' /var/lib/rancher/k3s/server/manifests/minas-traefik.yaml
sudo k3s kubectl -n traefik get deploy traefik -o jsonpath='{.spec.replicas}{"\n"}'
# BOTH must read 1. That verification IS the phase — doing the commit without it just
# moves the drift somewhere less visible.
```

⛔ If the cutover is instead rolled back, the `replicas: 1` commit must be reverted too.

### Rollback, still armed

| | |
|---|---|
| artifacts | `/storage2/backup/traefik-cutover-20260810T062431Z/` |
| `acme.json` known-good | sha256 `31f2b822…`, `root:root 0600`, 128998 bytes — **unchanged** through the whole cutover |
| docker container | `e230f30a9d3f…`, `Exited (0)`, image digest `9c3b91d5…` |

⛔ Rollback is `docker start <id>` — **never** `docker compose up`, which rebuilds the
container from today's file and applies every drift accumulated since it was created.
Procedure in `TRAEFIK-CUTOVER-RUNBOOK.md` §2.

---

Read **`TRAEFIK-CUTOVER-RUNBOOK.md`** for the procedure of record; it supersedes the "NO-GO"
account below. Read `INGRESS-ARCHITECTURE.md` too — it explains why there are two traefiks
on purpose, and that is still true: the pelargir edge is untouched by this cutover.

**Both blocking CRITICALs are CLOSED** (durable-promotion phasing, and `acme.json` rollback
protection — see the runbook §1 and §2). **The Pod has now run**, twice, as canaries, while
docker kept serving:

- All **26 hostnames reproduced the baseline exactly** on an alternate port, with the correct
  certificate per SNI, a working port-80 redirect, and **23 routers all `@file`, zero
  `@docker`** — so dropping the docker provider loses nothing.
- The **Cloudflare token was proven working today** against Let's Encrypt *staging*: fresh
  account, real DNS-01 TXT written and cleaned via the Cloudflare API, certificate issued.
  That matters because the live wildcard expires **Sep 16** and traefik renews at 30 days
  remaining — the first unattended test would otherwise have been **~Aug 17**.
- The production `acme.json` was **byte-identical before and after** both canaries.

**State:** ns `traefik`, the `traefik-env` Secret and the Deployment are now DEPLOYED, with
the Deployment inert at **`replicas: 0`**. Canaries are deleted and their state removed. All
26 hostnames still serve from docker (`e230f30a9d3f`). What remains is an outage window.

⛔ **Three corrections to what was previously recorded here:**
1. `acme.json` holds **10 certificates**, not one wildcard — including 7 for
   `roadmastertransport.io`, a zone with no routes on this host. Rollback restores the whole
   file; never reason about a single certificate.
2. The access log is **CLF, not JSON**. `accessLog: format: json` in `traefik.yml` is a static
   key in a dynamic file and is silently ignored. The manifest now sets
   `--accesslog.format=json`, without which the required router correlation is impossible.
3. `TRAEFIK_BASIC_AUTH_CREDS` is **dead config** carrying a junk placeholder — faithfully
   copied from the live docker container, which also carries it unused. `traefik.yml`
   hardcodes the real bcrypt hash. Remove it; never wire it up.

The original NO-GO analysis is kept below because each trap it describes is real.

### ✅ Already done, do not redo

- Manifest `manifests/traefik.yaml`, namespace, and delivery entry (`minas-traefik.yaml`).
- Secret `traefik-env` in ns `traefik` — values read from the live container into sops.
- ⛔ **Image already pulled into containerd and verified by digest.** It was ABSENT; without
  this the first pull happens *during* the outage.
- ✅ **Only pelargir needs a rebuild** — verified, not assumed: every pending non-doc change
  is pelargir-delivered, and `traefik-routes.nix`/`system.nix` are unchanged since minas'
  checkout. ⚠️ Re-verify this at cutover time rather than trusting it.
- **Baseline captured**: `/storage2/backup/traefik-precutover-baseline-20260810T051832Z.txt`
  — all 26 hostnames with their REAL codes plus cert identity (Let's Encrypt YR1, subject
  `saldivar.io`, SANs `saldivar.io` + `*.saldivar.io`, notAfter Sep 16 2026).
  ⛔ Acceptance is reproducing THAT list, never "a 200": five names are healthy at 401, four
  at 302, two at 307, one at 303. `dungeon.saldivar.io` is **000 and was already dead** —
  its backend is unreachable from the host itself. Not a regression.
- **Live docker identity, recorded so it is not guessed later**: name `traefik`, id
  `e230f30a9d3f`, PID `3638549`. ⚠️ The repo also says `traefik2` in places; the container
  was renamed 2026-08-07. Re-record the id at cutover; do not trust either name.

### ✅ TWO CRITICALS — BOTH CLOSED 2026-08-09. Resolutions in `TRAEFIK-CUTOVER-RUNBOOK.md`

Kept in full because the reasoning explains two traps that will recur on any singleton
service with shared on-disk state.

**1. A committed `replicas: 1` can resurrect the Pod after a rollback.**
Scaling imperatively and committing `1` in the same breath is incoherent: if the manifest
declaring `1` is delivered, rolling back by scaling the API object to 0 leaves pelargir's
activation reasserting `1` on the next checksum change or k3s restart — starting the Pod
*beside* docker, with competing ingress rules and two writers on `acme.json`.
✅ Fix: keep the DEPLOYED declaration at `0` through cutover, scale imperatively, accept, and
only then a **separate durable-promotion phase** that commits, deploys, and verifies both the
installed manifest and the live Deployment read `1`. A rollback must also revert any unshipped
`replicas: 1` commit. (This fleet already has 15 services in the opposite drift state; do not
add a worse one.)

**2. Rollback does not restore the shared `acme.json`.**
Both runtimes mount `/etc/letsencrypt` read-write. Sequential ordering prevents *concurrent*
corruption but does nothing about a failed issuance, account update or partial write made by
the k8s process before rollback — docker then inherits the damaged file, and that single
wildcard is the TLS dependency for **all 26 names**.
✅ Fix: after docker is stopped and proven gone, take a quiescent byte-for-byte copy of
`acme.json`; record sha256, ownership, mode and certificate fingerprint. On rollback: prove
the Pod's container task exited, preserve the failed copy for diagnosis, restore the known-good
file atomically with identical metadata, re-validate, and only then `docker start` the recorded
container id.

### Also raised, all accepted

- **Status codes alone do not prove correct routing.** Unrelated apps return the same 200/30x/401.
  Add per-SNI certificate fingerprint checks, the port-80 → HTTPS redirect, and correlate each
  request with its expected **`@file` router** in traefik's access log — this repo already
  records that the router identity field is the best evidence of which router won. `traefik.saldivar.io`
  must still be **401**; a 200 there means basic-auth was lost.
- **"Nothing listening" is runtime-specific.** `ss` sees docker-proxy's socket but is blind to
  CNI hostPort DNAT. Prove docker's exit by recorded id/PID (`Running=false`, `Pid=0`, no NAT
  rule, no docker-proxy socket); prove the Pod's exit through the CRI, not `ss`.
- ⛔ **This Pod has NEVER STARTED.** Staging at `replicas: 0` hides image-unpack, mount,
  dynamic-config-parse and credential failures until every hostname is already down, and a
  broken Cloudflare token would be masked by the existing certificate until renewal.
  ✅ Strongly consider a **canary first**: same digest on alternate ports, an ISOLATED copy of
  the ACME store (never the shared one read-write), issuance disabled — proving it starts,
  mounts, parses and authenticates before anything goes down.

### Three independent reviews produced all of the above

Two on the manifest and architecture, one on the cutover plan. Each found real defects,
including two claims of mine that were simply wrong (pelargir DOES do ACME, via cert-manager;
the inter-site transport is Tailscale, not WireGuard). ⛔ **Both houses' ACME uses the SAME
Cloudflare token** — verified identical by hash, whole-zone rights. Revoking it stops renewal
at both sites. Owner accepted this for now; per-site least-privilege tokens remain the better
end state.

## ⛔ A NEW NAMESPACE + ITS SECRET IN ONE COMMIT MAKES `nixos-rebuild switch` FAIL

Hit 2026-08-09 staging immich. The switch returned **exit status 4** and looked like a
broken deploy. It was not, and the end state was correct — but the failure is real, it will
recur for every new namespace, and it is worth recognising instantly.

`k3s-apply-secrets` applies the rendered Secret manifests, and the namespaces they target are
created by **k3s auto-deploy** from `manifests/namespaces.yaml`. Nothing orders those two. On
the deploy that introduces both, the unit runs first and dies:

```
Error from server (NotFound): error when creating ".../cluster-apps-secrets.yaml":
namespaces "immich" not found
```

✅ **It self-heals**: the unit has `Restart=`, and by the retry auto-deploy has created the
namespace — the next run logged `secret/immich-postgres created` and finished. Verified after
the fact: unit `active` with `NRestarts=1`, Secret present with all three keys.

⚠️ **But `nixos-rebuild switch` still exits non-zero**, so a deploy that actually worked
reports failure. That is the shape of thing this fleet keeps getting burned by — a signal
that cries wolf teaches you to ignore it. If you see exit 4 right after adding a namespace,
check `systemctl status k3s-apply-secrets` and the Secret before assuming damage.

⛔ And do NOT conclude from the self-heal that ordering is safe. If the unit ever loses its
`Restart=`, or the namespace takes longer than the retry window, the Secret is silently never
applied and the workload fails at cutover with what looks like a credential problem.

## ⛔ ONE COMMIT, TWO HOSTS — the mistake that caused the only outage

A migration commit touches files owned by **different hosts**, and rebuilding one does
not deploy the other:

| file | delivered by |
|---|---|
| `minas-tirith/traefik-routes.nix` | **minas'** activation — the route |
| `pelargir/manifests.nix` | **pelargir's** auto-deploy dir — the Pod |

On 2026-08-07 a `nixos-rebuild switch` on minas alone published the ten routes while
their Deployments did not exist. Traefik picked the dead k8s router for three hostnames
and `requests`/`overseer`/`lidarr` served **502 for about two minutes** while their
docker containers were healthy the whole time. Recovery was deleting the ten
`k8s-*.yml` files by hand.

**Rebuild pelargir FIRST, confirm the Deployments, then rebuild minas.**

This is now survivable rather than fatal: generated routers carry **`priority: 1`**, the
lowest, so a `<svc>@docker` router (default priority = rule length, ~26) always wins
while its container exists, and the k3s route takes over the instant it stops. Installing
a route before its Pod exists is therefore harmless. ⚠️ That guarantee dies if anyone adds
a catch-all router — the only other explicit priorities are 90/100 on
`dungeon.saldivar.io`.

---

## plex: MIGRATED 2026-08-08 — what was proven, and the one thing it exposed

Cut over cleanly. Evidence, so nothing here rests on assertion:

| gate | result |
|---|---|
| shutdown | `docker stop -t 120` exited **0 in 3 s**, leaving no `-wal`/`-shm` — both databases checkpointed cleanly, so the 120 s grace is ample |
| rollback dumps | taken **as uid 1000** (root would leave root-owned WAL sidecars) and VALIDATED with plex's own `Plex SQLite`: `integrity_check ok`, 82 tables, page_count 485730, `metadata_items` **47290** — matching baseline exactly. In `/storage2/backup/plex-cutover-20260808` |
| Service takeover | ClusterIP stayed **10.43.57.77** because the replacement pins it; selector went live, manual EndpointSlice replaced by the pod at 10.42.1.95 |
| identity | same `machineIdentifier c47f45b5…` — plex kept its identity and did NOT re-register |
| consumer | tautulli's pod reaches `http://plex:32400` by bare name and gets that same identifier |
| hostPort | `/identity` 200 from another machine |
| ingress | `plex.saldivar.io` → **401**, matching baseline |
| GPU accounting | `nvidia.com/gpu` now **jellyfin=1, plex=1** against allocatable 2 — the pair the device plugin was sized for, accounted for the first time |
| remote access | plex.tv still advertises `https://plex.saldivar.io:32400` and the public `99-64-240-101:32400` unchanged, `publicAddressMatches` and `relay` unchanged. Only the useless *local* address changed, from the docker-internal `172-16-1-17`/`172-16-2-12` to the pod's `10-42-1-95` — both equally unroutable from a LAN client |

### ✅ ANSWERED 2026-08-08: plex remote access IS direct and working — PROVEN externally

**USE PELARGIR AS THE EXTERNAL VANTAGE POINT.** This was the missing tool for the whole
question. pelargir is at a different site with its own public IP (`216.9.25.189` vs minas'
`99.64.240.101`) and routes to minas' public address over the **open internet**
(`via 10.0.0.1 dev eth0`), not through Tailscale or WireGuard. So a request from pelargir
to minas' public IP is a genuine inbound-from-the-internet test.

Results:

| from pelargir → 99.64.240.101 | result |
|---|---|
| `tcp/32400`, `tcp/443`, `tcp/80` | **OPEN from the internet** |
| `https://99.64.240.101:32400/identity` | **200** |
| `https://99-64-240-101.<hash>.plex.direct:32400/identity` — the exact URL plex.tv advertises, TLS + SNI and all | **200** |

The port forward works, DNS works, the plex.direct certificate path works. Nothing here
needs fixing.

⚠️ TWO SIGNALS THAT LOOK ALARMING AND ARE NOT. An earlier version of this section read
them wrong and concluded, incorrectly, that everything outside was Relay:

- `MyPlex: attempted a reachability check but we're not yet mapped.` is a **startup
  transient**, not a standing failure. Every occurrence is ~2 s after a server start
  (08:44:06→08:44:08, 07:49:32→07:49:34, 23:10:35→23:10:38, 18:24:22→18:24:24): plex.tv's
  pubsub pings before plex has finished establishing its mapping. Check the timestamps
  against `Plex Media Server v…` start lines before reading it as broken.
- `[PlexRelay] Allocated port NNNNN for remote forward to 127.0.0.1:32401` is **proactive**.
  Plex stands up a relay forward whenever Relay is enabled; its presence says nothing
  about whether traffic uses it.

Also do not treat "no public client IPs in the logs" as proof nobody connects directly.
Plex logs `[::ffff:IP]` for TLS/SNI events and similar, not for every request, so it is
not a census of clients.

⚠️ And do not trust probes from the WORKSTATION. Its default route is the WireGuard
tunnel, so `nc` to minas' public IP hairpins inside the far network and reports OPEN
regardless. That is what makes pelargir valuable: it is genuinely outside.

#### Original note, kept because the reasoning about test paths still applies

Every test path from the workstation goes **through WireGuard**. Verified rather than
assumed: the Mac's `192.168.4.3` sits on `utun6`, and `route -n get` shows the default
route, the route to `10.0.1.6` AND the route to the public IP `99.64.240.101` all using
that tunnel. So `curl https://99.64.240.101:32400` returning 200 proves a hairpin through
the VPN plus a NAT loopback — **not** that the internet can reach it.

⚠️ Two things were briefly recorded as evidence and are NOT:

- A client at `192.168.4.3` is the **workstation on WireGuard**, not a phone off-LAN. Any
  session logged from that address is an inside-the-tunnel test.
- A "playback from the phone" only counts if WireGuard is **OFF** on the phone. On the
  VPN it is indistinguishable from a LAN client.

What IS established: public DNS is correct (`plex.saldivar.io` →
`red.orleans.io` → `ddns.red.orleans.io` → `99.64.240.101`), plex.tv advertises TWO
direct non-relay connections plus a relay fallback at
`173-230-133-167…:8443`, and `publicAddressMatches` is true. Suggestive, not proof —
plex.tv advertises what the server reports.

The only test that settles it: **phone on cellular, WireGuard OFF**, play something, and
check Settings → Remote Access reads "Fully accessible outside your network".

Note also `ManualPortMappingMode="1"` with **no `ManualPortMappingPort` set**, so plex
assumes external port 32400; the inbound path depends on the router forwarding it.

Still worth doing: a real playback from **outside** the LAN, confirmed direct rather than
Relay. Everything measurable from inside the network passed.

### ✅ FIXED 2026-08-08: plex had never been hardware transcoding — on EITHER runtime

**Resolved by setting `LD_LIBRARY_PATH=/usr/local/nvidia/lib` on the plex container.**
Verified on a real client playback: zero `Cannot load libcuda` in the log,
`videoDecision="transcode"` on an HEVC source, and `transcodeHwDecoding="nvdec"` +
`transcodeHwEncoding="nvenc"` recorded in `Plex Transcoder Statistics.log` at 01:53:21,
matching the session end in the server log to the second.

Why that works when everything else already looked right: on NixOS the ldconfig cache
resolves `libcuda.so.1` **indirectly**, to a `/nix/store/...` path, while
`/usr/local/nvidia/lib` holds the same libraries behind direct relative symlinks.
Pointing the loader at that directory first bypasses the indirection. jellyfin's ffmpeg
was unaffected, which is why it worked all along and made this look cluster-wide when it
was not.

⚠️ If a future lsio-based GPU workload silently transcodes on CPU, try this first.

The diagnosis below is kept because the *elimination* is what made the fix findable.

#### The original finding, and what was ruled out

A transcode in the Pod logs `Cannot load libcuda.so.1` / `Could not dynamically load CUDA`,
so plex silently falls back to CPU. **This is PRE-EXISTING, not caused by the migration**,
and the timeline is unambiguous rather than inferred:

- plex logs in **UTC**. Docker ran `2026-08-06T23:10:33Z → 2026-08-08T07:45:40Z`.
- The rotated docker-era log `Plex Media Server.1.log` holds **6** of these errors, all at
  `Aug 08 02:43 UTC` — 19:43 PDT on Aug 7, **five hours before** docker was stopped.

So the same failure happens under docker and under k3s, on the same host CDI spec. Note
`nvidia-smi` works inside the Pod and `jellyfin`'s ffmpeg genuinely does use CUDA there
(a real NVENC + `tonemap_cuda` transcode was measured at 20.7x realtime), so this is
plex-specific rather than a cluster GPU problem. `HardwareAcceleratedCodecs="1"` is set,
and `libcuda.so.1` is present in the Pod with four entries in the ldconfig cache — which
is what makes it puzzling and worth its own investigation.

⚠️ `Plex Transcoder` cannot be driven by hand to test this: it is a hardened build that
rejects `-init_hw_device` with `Operation not permitted` and refuses arbitrary output
files. Drive a transcode through the API, or read the log.

## ⛔ RECREATING A LONG-RUNNING CONTAINER APPLIES CONFIG IT WAS NEVER RUNNING

A container keeps the configuration it was CREATED with. The compose file it came from
has been edited many times since. So `docker compose up -d <svc>` does not "restart" it —
it rebuilds it from whatever the file says **today**, and every drift accumulated in
between lands at once.

This bit on 2026-08-08 while fixing btbooks' 502. The one-label change was correct and
verified, but recreating `deluge-books` also applied a `NAME_SERVERS` value that had
drifted out of sync with its sibling:

| | first resolver | healthcheck | result |
|---|---|---|---|
| `deluge-vpn` | `8.8.8.8,…` | passes | healthy, always has been |
| `deluge-books` | `209.222.18.222,…` (PIA) | **fails** | UNHEALTHY |

PIA's resolver answers `google.com` with **AAAA records only**, the tunnel carries no
IPv6, and the image's healthcheck is
`curl http://localhost:8112 && curl https://google.com` — so the second half failed.

⚠️ And the consequence is not obvious: **traefik's docker provider drops UNHEALTHY
containers**, so `btbooks.saldivar.io` served 404 while deluge itself was happily
answering 200 on 8112. A 404 there means "no router", which can mean "container marked
unhealthy" — not just a bad rule.

Fixed by matching the working sibling (`8.8.8.8` first). Two things to carry forward:

- **This applies to ROLLBACK too.** `docker compose --profile migrated up -d <svc>` on any
  migrated service recreates it from today's file, not from what it was running when it
  was stopped. A rollback is therefore not guaranteed to reproduce the last known-good
  container. Check the compose entry before relying on one.
- Any of the remaining docker services may carry the same latent drift. It stays invisible
  until something recreates them — which a migration always does.

## ⛔ hostPort IS NOT A FENCE — IN EITHER DIRECTION

The handoff already said `docker start` can bypass a k8s hostPort, because CNI implements
it as PREROUTING DNAT rather than a bound socket. **The converse is equally true and was
nearly missed**: a Pod can start while the docker container is running. A plex manifest
was committed at `replicas: 1` on the theory that "docker and the Pod cannot both bind
32400"; they can, and any k3s restart or pelargir switch before the planned `docker stop`
would have put two writers on the same 497 MB database.

Staging at `replicas: 0` is the conservative choice and is **safe as long as the manifest
declares no Service**. What took flaresolverr down was staging its replacement *Service*
over the bridge, not the zero-replica Deployment.

## ⛔ REPLACING A BRIDGE SERVICE IS A DELETE-AND-RECREATE ACROSS TWO AddOns

A bridge Service is owned by the `minas-docker-bridges` AddOn. k3s' deploy controller
prunes objects removed from a changed manifest, and the auto-deploy directory is applied
in **filename order** — `minas-docker-bridges.yaml` sorts **before** `minas-plex.yaml`.
So on the deploy that removes a bridge entry, the bridge AddOn deletes the Service first
and the workload's AddOn then creates it fresh. That is a new **ClusterIP**, not an
in-place patch.

Consumers here resolve by name, so DNS mostly covers it — but CoreDNS caches 30 s and
anything holding the old address fails until it re-resolves. Pin the existing ClusterIP
explicitly in the replacement Service. (An earlier comment in `manifests.nix` asserted the
opposite ordering; check with `sort`, not from memory.)

## ⛔ DURABLE STATE BELONGS IN GIT — 15 services are one k3s restart from an outage

Every migration before jellyfin shipped its manifest at `replicas: 0` and then ran
`kubectl scale --replicas=1` at cutover. **That leaves the manifest and the cluster
permanently disagreeing**, and it is a live landmine rather than untidiness.

k3s auto-deploy re-applies a manifest when its file **checksum changes** *or* when the
**server restarts**. So the imperative `1` survives only until the next edit to that
file or the next k3s restart — at which point the declared `0` is reasserted and the
service goes down, with nothing in git to explain why.

Verified on the live cluster: `kavita`, `komga`, `calibre` and `tautulli` all declare
`replicas: 0` in their manifests while running at 1. (This is also why hand-scaling
*survives* an ordinary `nixos-rebuild` — `install` of byte-identical content does not
change the checksum. That is the property that has been hiding the problem.)

`jellyfin` does not join them: its cutover raised `replicas` and cleared the CronJob's
`suspend` **in a second commit**. Fixing the other 15 is its own change — do it before
the next k3s restart, not during a migration.

For a CronJob the same mistake is worse than an outage, because it is **silent**: a
reverted `suspend: true` simply stops taking backups, and the marker staleness gate
would not say so for two days.

## ⛔ `--request-timeout` SILENTLY DISABLES kubectl's in-cluster config

Cost an hour on the jellyfin quiesce job, and it fails in a way that points nowhere near
the cause:

```
The connection to the server localhost:8080 was refused - did you specify the right host or port?
```

…with the ServiceAccount token mounted, `KUBERNETES_SERVICE_HOST=10.43.0.1` set, and
`kubectl auth can-i` confirming the RBAC. kubectl only falls back to **in-cluster**
configuration when the merged kubeconfig **equals the defaults**. Any global flag that
shapes the client config — `--request-timeout` among them — makes the merged config
differ, so kubectl returns *that* config, pointing at `localhost:8080`, and never reads
the token at all.

Bisected: the identical command without the flag prints `Using in-cluster configuration`
and succeeds. Bound an in-cluster job with the **Job's `activeDeadlineSeconds`** instead
— it is also the bound that matters, since it terminates the pod and releases
`concurrencyPolicy: Forbid`.

## Deploying — the tooling actually works like this

`nixos-rebuild` **cannot be driven from the Mac.** `nix run|shell nixpkgs#nixos-rebuild`
resolves to the **x86_64-linux** build, whose wrapper's Python interpreter is a Linux ELF
binary; the kernel cannot exec it, bash falls back to reading the Python as shell, and it
dies on `syntax error near unexpected token 'lambda'`. It is not a config problem and no
flag fixes it.

Both hosts have a working native `nixos-rebuild`. Deploy by staging the tree and building
on the host:

```sh
rsync -a --delete --exclude='.direnv' --exclude='result' ./ minas:/home/edgar/nixos-config/
ssh minas 'sudo nixos-rebuild switch --flake /home/edgar/nixos-config#minas-tirith'
```

Use the **absolute** path, never `~` — `~` expansion under sudo is what left four media
services on blank configs (see RESTORE-RUNBOOK.md). `dry-build` first is free and catches
eval and permission problems before activation.

### ⛔ COMMIT, then rsync — and never trust "Done." as evidence

This went wrong **twice on 2026-08-07**, the second time after the lesson had already been
written into a commit message:

- `rsync` copies the WORKING TREE. Doing it before committing deploys uncommitted state,
  so the running system and the repo silently diverge. One change reached a host only as a
  dirty-tree modification and appeared to be "deployed" while its commit sat unpushed.
- Committing and then **forgetting to rsync** is worse, because the switch still succeeds:
  it redeploys the config the host already had, prints `Done. The new configuration is …`,
  and looks identical to a real deploy. Four commits were reported as landed while the
  hosts were 2 and 5 commits behind — including a digest pin that was still a tag on disk.

⚠️ **"Commit first, then rsync" is not sufficient advice, and saying it did not stop this
happening a THIRD time.** The reason is that testing legitimately requires rsyncing
uncommitted code — you build and run the thing on the host *before* you are willing to
commit it. So the real rule has three beats, and the third is the one that gets skipped:

```
rsync (uncommitted)  ->  build & test on the host  ->  commit  ->  RSYNC AGAIN  ->  switch
                                                                   ^^^^^^^^^^^
```

Skipping the second rsync leaves the host running a *dirty tree* whose content happens to
be right, while `git` on that host points at an older commit. The change works, so nothing
looks wrong — until the next switch quietly deploys something else, or a later reader
believes the host matches the repo. (Note a dirty tree also hashes differently from the
identical clean commit, so the next switch rebuilds ~13 derivations to produce a
functionally identical system. Harmless, but it is the tell.)

**A no-op switch and a real one are indistinguishable from the output.** What actually
distinguishes them:

```sh
# 1. after committing, stage AGAIN
git commit … && rsync -a --delete --exclude='.direnv' --exclude='result' ./ HOST:/home/edgar/nixos-config/
# 2. prove the host has what you think it has
ssh HOST 'cd ~/nixos-config && git log --oneline -1 && git status --short | wc -l'   # want 0 dirty
# 3. a real deploy BUILDS something
ssh HOST 'cd ~/nixos-config && sudo nixos-rebuild dry-build --flake …#HOST 2>&1 | grep -c "\.drv"'
# 4. after switching, assert the CHANGE, not the exit code
ssh HOST 'grep -o "k8s-device-plugin[@:][^ ]*" /var/lib/rancher/k3s/server/manifests/…'
```

⚠️ **Both hosts.** A change to a `minas-tirith/manifests/*.yaml` file is delivered by
**pelargir**, so minas can be perfectly up to date while the manifest never ships. See the
two-hosts warning above.

pelargir's checkout is often stale while its **running system is current** — it gets
deployed from elsewhere. Judge the delta by the derivation count in `dry-build`, not by
`git log`.

---

## ⛔ REPLACING A BRIDGE HAS NO INERT WINDOW — learned the hard way 2026-08-07

Every other migration can be staged at `replicas: 0` and deployed harmlessly. **That is
false when the service being migrated currently has a docker bridge**, and flaresolverr
proved it by going down.

The bridge Service and the new real Service have **the same name** — they must, because
the whole point is that consumers keep resolving the same bare name. So applying the
manifest **overwrites** the selectorless bridge Service, adding a selector. The manual
EndpointSlice is pruned along with it, the selector matches a Deployment at `replicas: 0`,
and the Service resolves to **nothing**. Consumers get connection refused from the moment
of the "inert" deploy.

Observed exactly: prowlarr went to `curl` exit 7 against `http://flaresolverr:8191/health`
the instant pelargir applied the manifest, and stayed there until docker was stopped and
the Pod scaled up.

**For the three bridges that remain — `plex`, `deluge-books`, `deluge-vpn` — do NOT stage
at zero.** Either:

- stop the docker container FIRST, then deploy, then scale up immediately; or
- deploy and scale up in one motion, accepting a brief hostPort collision instead of a
  DNS black hole (only viable if the service publishes no host port).

This matters much more for the VPN pair than it did here: those containers are privileged,
carry a kill-switch, and their consumers are the *arr mesh's download clients.

Also note: `hostPort` forces the ordering anyway. The docker container and the Pod cannot
both bind 8191, so docker must stop before the Pod can start — meaning "scale up fast" is
not available as a mitigation unless the port is free.

## The cutover procedure (validated over 13 migrations)

1. **`k3s crictl pull <digest>` FIRST**, for every image. Images live only in *dockerd's*
   store; containerd cannot see them. This is the long pole and it is non-disruptive, so
   do it well ahead.
2. Baseline every hostname **and record the actual status codes**.
3. Rebuild **pelargir** → Deployments land at `replicas: 0`. Inert.
4. Confirm the Deployments exist. This is what stops you stopping docker with nothing to
   scale into.
5. Rebuild **minas** → routes install. Inert, thanks to `priority: 1`. Re-verify the
   baseline here; nothing should have changed.
6. `docker stop`, verify `Exited`, then **check `-wal` files**.
7. `PRAGMA integrity_check` **as the container's own UID**, never as root.
8. Scale up, confirm each app's **own health endpoint**.
9. Verify ingress, and any `hostPort` **from another machine**.
10. Add `profiles: ["migrated"]` to the compose service, then confirm with
    `docker compose config --services` that it no longer appears.

### ⚠️ What `profiles: ["migrated"]` actually guarantees — it is NOT a fence

Cross-review (2026-08-07) correctly attacked the claim, written in every migrated compose
service, that profiling "means `docker compose up -d` **cannot** resurrect this
container". Precisely:

- ✅ **`docker compose up -d`** (the bulk case) skips it. Real, and the common accident.
- ✅ **Daemon restart / reboot** will not start it: every migrated container is `exited`
  with `restart: unless-stopped`, and `unless-stopped` means an explicitly-stopped
  container stays stopped. Verified across all 13.
- ❌ **`docker start <name>` bypasses Compose entirely.** The container still exists with
  its full configuration, including its hostPath bind. Nothing stops it.
- ❌ **`docker compose --profile migrated up -d <svc>`** starts it, by design — that IS
  the rollback lever.

So profiles are a guard against ACCIDENT, not a mutual-exclusion mechanism. For SQLite
services the exposure is one deliberate command away from two writers on one file; for a
Postgres cluster it is worse, because `postmaster.pid` is namespace-blind and provides no
cross-runtime interlock either.

If a genuine fence is needed — and it is, before migrating a database — the container has
to be **removed** (after recording its configuration and digest), with the definition kept
in a rollback-only compose file that routine commands never touch.

### ⚠️ The `-wal` gate is weaker than it looks

The old text said `-wal` absence proves a clean checkpoint. On the `media` wave **it did
not hold**: `shelfmark` was SIGKILLed at docker's 10 s timeout (exit 137), `overseerr`
exited 1, `wrapperr` exited 2, and six databases kept a WAL — overseerr's was 4.1 MB.

None of it was damage. A WAL is normal SQLite recovery state and replays on next open.
What actually matters is:

- **`integrity_check` as the right UID** — the real proof. All nine returned `ok`.
- **WAL ownership**, because that is what a Pod must be able to open. Here it was already
  correct: `edgar:1000` for the *arr apps, `edgar:911` for animearr (confirming the
  `GUID` typo), `root:root` for overseerr whose Pod also runs as root.

Run `PRAGMA wal_checkpoint(TRUNCATE)` in the same connection as the integrity check — as
the right UID — and the WAL folds in cleanly. Expect the main DB to **grow**
(`sonarr.db` +69 KB); compare sizes *after* the stop, not against the pre-stop baseline.

---

## Manifest invariants

- **Digest-pin the image**, keeping the registry host as written (`lscr.io`, `ghcr.io`,
  `docker.io` are not interchangeable).
- **`strategy: Recreate`** for anything with hostPath SQLite.
- **`hostPath type: Directory`** — the default type *creates* a missing path.
- **Only compose-declared env.** Restating image defaults freezes them.
- **`enableServiceLinks: false`**.
- **Do NOT set `runAsUser` on linuxserver.io images.** They run as **root** and drop to
  PUID/PGID via s6 themselves; forcing uid 1000 breaks their init. 9 of the wave's 10
  containers run as root — only `maintainerr` (compose `user: "1000:1000"`) gets a
  `securityContext`. Verify with `docker exec <c> id -u` rather than reading compose.
- **No `fsGroup`** — a no-op on hostPath (tested).
- **Preserve bugs verbatim.** `animearr`'s `GUID=1000` is a typo for PGID; because PGID is
  unset the image falls back to group **911**, which is why its config tree is `1000:911`.
  "Fixing" it chowns the tree on first start and breaks rollback. Likewise `UMASK_SET=18`
  (compose writes `022`; YAML reads leading-zero octal, docker passes decimal 18) and
  radarr's `TZ=PS`.
- **Service names must match docker ALIASES**, not container names — `tautulli` **and**
  `media-tautulli-1`; `overseerr` **and** `overseer`.
- **Probes: verify the endpoint live first.** `sonarr` and `animearr` answer **401** at
  `/`, so a guessed readiness path leaves the Pod permanently NotReady. Verified paths:
  `/ping` (prowlarr, sonarr, radarr, lidarr, animearr), `/status` (tautulli),
  `/api/v1/status` (overseerr), `/api/health` (shelfmark), `/` (wrapperr), and exec
  `/opt/app/healthcheck.sh` (maintainerr).
- **Routes are declarative** in `traefik-routes.nix`, same commit as the manifest.

---

## Docker-side consumers of a migrated name — check BEFORE the cutover

Kubernetes bridges solve **Pod→docker** only. Docker's embedded DNS cannot discover a k8s
Service, so anything still on docker that references a migrated service by bare name
breaks at cutover — and it breaks silently.

`D15`'s reference graph **missed one**: `media-tracearr-1` held
`tautulliUrl = "http://tautulli:8181"` in its Postgres `settings` table, where no env or
config-file scan would find it. Scan application **databases**, not just env and config.

The fix, now in `docker/media/docker-compose.yaml` and worth reusing:

```yaml
    dns:
      - 10.43.0.10        # CoreDNS ClusterIP
      - 1.1.1.1           # fallback for external names
    dns_search:
      - media.svc.cluster.local
```

The bare name resolves through the search domain, so **the application's stored config
never changes** — and it is already what it will resolve by once tracearr itself moves
into `media`. `traefik` has run this way for some time; same mechanism, proven.

Costs the container the ability to resolve *other docker container names*. Verify it
needs none before applying.

⚠️ `hostPort` was not an option for tautulli: **`calibre` already publishes 8181** on the
host. Check for host-port collisions before reaching for that lever.

---

## Corrections to earlier documents

- **`prowlarr` does NOT depend on `readmeabook`.** D15 recorded that edge; the live
  `Applications` table holds `radarr`, `sonarr`, `lidarr`, `animearr` and a **dead
  `readarr` at `http://readarr:8787`** (removed 2026-08-06). The only `readmeabook`
  string is in the **`History`** table — a record of past grabs, not configuration.
  So readmeabook needs **no** port published to stay bridgeable, and the open decision
  about publishing 3030 is void. Distinguish config tables from history tables.
- **The `media` wave was 10, not D15's 11** — `readmeabook` was excluded and, per the
  above, has no inbound edges forcing it in.
- **Namespace question resolved**: `shelfmark` went to `media` because its config holds
  `PROWLARR_URL: prowlarr:9696` and `EXT_BYPASSER_URL: http://flaresolverr:8191`. Its
  `QBITTORRENT_URL` is already a host IP, which a Pod reaches fine.

- ⛔ **`readmeabook` DOES have edges — the claim below was wrong.** This file previously
  said "`readmeabook`, having no edges, is free to land in `books`". Its live
  `configuration` table says otherwise, and all three are OUTBOUND bare names:

  | key | value | where that lives |
  |---|---|---|
  | `audiobookshelf.server_url` | `http://audiobookshelf:80` | k3s, **books** |
  | `prowlarr_url` | `http://prowlarr:9696` | k3s, **media** |
  | `download_clients[0].url` | `http://gluetun:8080` | **docker**, no bridge |

  They are CONFIG, not history — verified by which table they live in (`configuration`,
  versus the prowlarr URLs that also appear in `download_history`). This is precisely the
  D15 trap this document already records for `media-tracearr-1`'s `tautulliUrl`: the
  earlier scan checked env and config files, not the application DATABASE.

  So NO namespace works unaided: `books` resolves audiobookshelf but not prowlarr,
  `media` the reverse, and neither resolves `gluetun`. It needs `books` **plus** an
  ExternalName alias for prowlarr and a selectorless bridge for gluetun
  (Service port 8080 → `10.0.1.6:8880`, since gluetun publishes 8080 as host 8880).

  ⚠️ The general lesson, since it will recur: **re-scan the application database at
  migration time.** These edges are added through a UI long after any earlier survey.

---

## Backups — read this before trusting them

`backup-root-data.service` on minas, daily ~00:05. It had **three** independent defects,
all fixed 2026-08-07, and all of them failed *quietly*:

1. **It had been dead for a day.** The script runs `set -euo pipefail`, and the k3s
   Postgres discovery loop did `img=$(crictl inspect | grep ... | head -1)`. grep exits 1
   when it matches nothing, pipefail promotes that, and `set -e` killed the whole script.
   Every non-Postgres container aborted the entire backup. It broke the instant the first
   k3s workload landed on minas. Guarded with `|| true` in five places — including both
   docker loops, which would have failed identically once `nextcloud-db` and
   `immich-postgres14` migrate.
2. **komga had never once been dumped.** The list named
   `/usr/local/etc/komga/database.sqlite`; the database has always been at
   `/etc/komga/config/`. `[ -f "$db" ] || continue` skipped it silently while reporting
   success. A missing path is now a WARNING plus a `degraded` marker.
3. **It dumped as root.** `sqlite3 <db>` opens read-WRITE and CREATES `-wal`/`-shm` when
   absent — the state right after an app stops or checkpoints. Root-owned sidecars make a
   uid-1000 Pod unable to open its own database. Dumps now drop to the database's uid via
   `setpriv`, staging through a directory owned by that uid; validation and the final move
   stay with root, which then `chown`s the artifact back to `root:root`.

### ✅ Acceptance criterion 6 is SATISFIED (2026-08-07)

The plan requires "a k8s-aware backup with database dumps runs and is **restore-tested**
BEFORE the first stateful migration", and notes that v1 deferring this was already a
mistake. Nextcloud is that first stateful migration, so this was the gate.

Proven end to end, with a real 124,415-row database rather than a synthetic one:

1. Live docker `nextcloud-db` → the docker loop's dump → restored into an **isolated**
   k3s Postgres (emptyDir, same pinned digest, dumps mounted read-only).
2. That restored cluster → dumped again **by the k3s loop** → restored into a *second*
   isolated cluster.

Both restores exited 0 with a single benign `role "postgres" already exists` — inevitable
when restoring a `pg_dumpall` into a cluster that already has the superuser. Every count
matched the live baseline both times: 2 databases, 14 roles, 103 tables,
`oc_filecache` 124415, `oc_users` 4, `oc_storages` 5.

⚠️ What this does NOT prove: the k3s loop has **no counterpart to the docker
never-dumped/stale walk** (`system.nix` checks `docker ps -a`; the k3s branch simply skips
absent containers). A k8s database that silently stops being dumped will age forever
without a marker. Close that before relying on it for a migrated database.

**15 databases are dumped now, against 2 before.** Verify a run with:

```sh
ssh minas 'sudo systemctl start backup-root-data.service; systemctl show backup-root-data.service -p Result --value'
```

Every line should read `as uid N`, and jellyfin should report `restarted ... after
consistent dump`.

⚠️ **`/storage` is NOT in the filesystem backup** (it covers `/etc /home /usr/local /opt
/srv` and the two container-storage trees). That is deliberate for ~98 TB of
re-downloadable media — but `/storage/Media/Library/metadata.db` is calibre's entire
library CATALOG and was protected by nothing. It is now dumped onto storage2. Check for
this class of thing when migrating anything whose real data lives under `/storage`.

⚠️ Only jellyfin needs quiescing (it holds `library.db` write-locked for its whole
runtime). Everything else was measured dumping fine against a RUNNING Pod via the online
`.backup` API — sonarr's 960 MB in 2.1 s.

**jellyfin's quiesce is LIVE as of 2026-08-07** and it is NOT a `docker stop` any more:

- a CronJob (`23:30 America/Los_Angeles`) deletes pod `jellyfin-0`;
- the StatefulSet recreates it, and its **initContainer** copies `library.db` **and its
  rollback journal** into `/storage2/backup/staging/jellyfin/current/` while the writer
  is absent by construction, then writes
  `/storage2/backup/staging/jellyfin/.status` = `staged <iso> bytes=<n>`;
- `system.nix` lists that staged copy as an ordinary sqlite entry, so it gets the full
  integrity_check + table-count + byte-floor gate before being promoted to
  `dumps/_storage2_backup_staging_jellyfin_current_library.db`.

Proven end to end on 2026-08-07: pod UID changed, capture refreshed, backup ran
`success` with no degraded marker, and the promoted dump **restore-tested** —
`integrity_check ok`, 8 tables, `TypedBaseItems` 104,007 **matching the live database
exactly**. That closes the design doc's last open gate.

⚠️ Note the marker is in the service's own **staging** tree, not `dumps/` — the capture
runs the application's own image as root, and mounting the shared dump directory into it
would give a third-party image write access to every other service's dump. Do not "tidy"
it back.

## Known-broken, pre-existing, NOT caused by the migration

- **`btbooks.saldivar.io` → 502.** `deluge-books`' traefik label says
  `loadbalancer.server.port: 9812`, but that is the **host**-published port; the app
  listens on **8112** inside the container, and a docker-provider route connects to the
  container IP. The bridge in `docker-bridges.yaml` is correct (it targets 9812 on
  10.0.1.6, the host) — only the traefik label is wrong. One-line fix, untouched here to
  keep the cutover scoped.
- **`prowlarr` holds a dead `Readarr` application** pointing at `http://readarr:8787`.
  Harmless, noisy; delete it in the UI.

---

## Credentials seen in session — rotate

Deliberately deferred, recorded so they are not forgotten:

1. **PIA credentials** — 5 files under `/home/edgar/git/docker`. Rotate, update, then
   recreate `deluge-vpn`, `deluge-books`, `gluetun` **one at a time**, confirming tunnel
   and non-local egress between each.
2. **`media-tracearr-1`** — `JWT_SECRET` and `COOKIE_SECRET` are set to the same short,
   guessable value in plaintext compose env (see `docker/media/docker-compose.yaml`), and
   its Tautulli API key sits in the `settings` table. **This repository is PUBLIC — the
   values are deliberately not written here.** Read them from the host.

⚠️ This entry originally quoted the literal password, and it reached a commit before the
push. That is the second time this class of mistake has happened here (the first leaked
PIA credentials). Documenting a credential to remember rotating it is still writing it
down: name the location, never the value.

### Full audit, 2026-08-08 — scope, and the two things that were wrong about it

**Nothing was ever published.** Verified, not assumed: `~/git/docker` has zero remotes,
zero remote-tracking refs and no upstream on its only branch, so it cannot have been
pushed. And no `.env`/`credentials`/`password`/`.key`/`token` file has existed in THIS
(public) repo at any commit; a search across every commit for an assignment with a
value-shaped RHS returns nothing. The `VPN_PASS`/`RMAB_JWT_SECRET` hits in these docs are
prose plus explicit `<redacted>` placeholders. The exposure is **plaintext at rest on
hosts we control**, not disclosure — treat it as hygiene, not incident response.

Credential-bearing files in the backup sources: the five `.env` under `~/git/docker`
(`traefik.env` ×2 copies carry the highest-value item, a Cloudflare DNS API token;
`authentik.env` carries the SSO signing key and a Google OAuth client secret;
`books/.env` carries PIA + `RMAB_*`; `output.env` is clean), two binhex
`openvpn/credentials.conf`, and under `/etc`: the k3s node password, 6 Minecraft RCON
envs, and a calibre TLS private key. `/var/lib/docker/volumes` and
`/var/lib/rancher/k3s/storage` were scanned and are clean.

**Bounding facts:** the backup never leaves the host (no syncoid/restic/borg/rclone, no
NFS export, no SMB share), and minas is *deliberately not a sops recipient* for
`cluster-apps.yaml`, so its host key sitting in the backup decrypts nothing shared. The
aggravating fact is `storage2` has `encryption=off`, so a disk leaving the building
carries all of it — and ZFS encryption is creation-time-only, i.e. a pool rebuild.

**Two errors in that audit, both worth remembering:**

1. A filename-shaped scan misses content-shaped secrets. `*.secret` (singular) missed
   `/usr/local/etc/readmeabook/config/.secrets`. Worse, the highest-value material is
   *inside application state*: tracearr's Tautulli API key in its `settings` table,
   readmeabook's Audiobookshelf token encrypted in its database, jellyfin API keys in
   `library.db`/`jellyfin.db`. Those are hostPaths **and** get nightly dumps into the
   snapshotted dataset. Rotating every `.env` value leaves all of it untouched.
2. `rsync --exclude` + `--delete` **protects** matching files on the receiver — that is
   exactly why `--delete-excluded` exists. So "just exclude `credentials.conf` from the
   backup" does not remove the copy already in the mirror; it **freezes it there
   permanently** and every future snapshot keeps capturing it. Excluding is not
   scrubbing. Prove any such change with `rsync -navi --delete` first.

⛔ **Rotation does not retire this on its own.** Rotating writes the *new* credential into
the next backup in plaintext. The loop only closes when the file stops being plaintext.

⛔ **`secretKeyRef` env vars are on disk too** — they land in containerd's container
metadata on the node. tracearr's JWT/COOKIE are therefore on disk today despite coming
from sops. "No plaintext secrets" needs Secret **volumes**, not env, to actually hold —
or an explicit, written decision that the node's disk is inside the trust boundary.
That decision has not been made; do not claim the goal is met until it is.

3. **`nextcloud-redis`'s `REDIS_HOST_PASSWORD`** — plaintext in
   `~/git/docker/docker-compose.yaml`, and **printed unmasked to a terminal on 2026-08-09**
   while locating the compose services. Functionally dead (the server has no `requirepass`,
   which is why its healthcheck failed 7161 times), and the container is now retired — but
   it was exposed, so retire the value rather than leave it in the file.
   ⛔ That was the FOURTH unmasked credential dump in this migration, all from the same
   cause: reading container or compose output without a redaction filter. Filter by default.

4. **MyAnonaMouse session cookie** — a literal `mam_id=` value embedded in
   `deluge-books`'s docker **`Cmd`** (visible via `docker inspect`, and therefore also in
   `/var/lib/docker/containers/*/config.v2.json` on disk and in the compose file). Rotate
   by re-issuing the session from MAM. Scanned all 12 remaining containers: **only
   deluge-books** carries a credential-shaped literal in `Cmd`/`Entrypoint`.

   ⛔ **A THIRD credential shape the audit missed.** Not a file named like a credential,
   not application state — a **command-line argument in container config**. The audit
   looked for files; this is an argv. When auditing, `docker inspect` `.Config.Cmd`,
   `.Config.Entrypoint` and `.Config.Env` are first-class targets, and every dump of them
   must be piped through a mask (`s/([=:])[A-Za-z0-9_.+\/=-]{8,}/\1<MASKED>/`) before it
   reaches a terminal. This value was printed unmasked while gathering migration facts —
   the third such slip in one session, all from the same cause: dumping container or log
   output without a redaction filter. Filter by default; do not rely on remembering.

   ⚠️ **It is also a functional requirement, not just a secret.** That `Cmd` starts
   `init.sh` in the background, waits 45s, verifies `tun0`, and then calls MAM's
   `/json/dynamicSeedbox.php` to register the current exit IP. MAM sessions are IP-bound,
   so **if the k3s translation drops this call, MAM access breaks after the first exit-IP
   change** — silently, and not at cutover but whenever PIA next moves the endpoint. The
   Pod must reproduce it, with the cookie coming from a sops Secret rather than argv.

### DECIDED 2026-08-08 (owner): wipe the pre-migration backups at the END

`/storage2/backup-2026-07-30` is a **298G** static pre-migration copy holding **13 `.env`
files** — the five docker ones, six Minecraft RCON envs, and two belonging to a different
project entirely (`~/PinCollector/infra/.env` and a `pincollector-config-backup` copy).

It sits on the `storage2` ROOT dataset, which has **zero snapshots** (`storage2@` count is
0; the 14 dailies are on `storage2/backup`). That makes it the one place a real scrub is
possible — plain directory, so `rm` genuinely erases the bytes. Everywhere else is either
live state that is needed or immutable snapshot bytes.

**Do not delete it during the migration.** It predates the rebuild and may be the only
copy of pre-rebuild state. Wipe it once the migration is complete, as the owner's
explicit decision — and re-check the `.env` inventory at that time rather than trusting
this list, which was accurate on 2026-08-08.

---

## Probes that give false answers on this fleet

- `ss` shows nothing for a k8s `hostPort` — CNI implements it as PREROUTING DNAT, not a
  listener. Test **from another machine**. docker-proxy *does* listen, so the two
  runtimes genuinely differ.
- NetworkPolicy tested through traefik looks unenforced — traefik pools connections and
  conntrack passes ESTABLISHED flows. Force a fresh TCP connection.
- VPN egress checks can fail on PIA's DNS rather than routing; test by IP.
- `docker stats` memory includes page cache and overstates by up to 6×.
- A Steam A2S probe gets no reply from palworld because `COMMUNITY=false`.
- **kubelet's `httpGet` sends the POD IP as the `Host` header**, so any app that validates
  Host (nextcloud's `NEXTCLOUD_TRUSTED_DOMAINS`) answers 400 to a probe that `curl` gets 200
  for. ⛔ Worse, a failing **startupProbe suppresses readiness and liveness entirely** — they
  never run — so it presents as a broken readiness probe. Set `httpHeaders: [{name: Host,
  value: <trusted host>}]`. Cost real downtime on the nextcloud cutover, 2026-08-09.
- **`psql -c` does NOT expand `:'var'`.** psql substitutes variables only when the SQL
  arrives on **stdin or from a file**; with `-c` the literal `:'var'` reaches the server and
  it answers `syntax error at or near ":"`. Verified 2026-08-09: `psql -v p=x -c "SELECT
  :'p';"` errors, the same statement on stdin returns the value. ⚠️ The failure reads like a
  broken *database*, not a broken command — and in a scripted gate whose result is a count,
  it presents as "no rows matched" rather than as an error. A known-file capture returned
  `NOT-IN-FILECACHE` for 20 of 21 rows this way; the row count looked healthy and only
  inspecting a field exposed it. Feed SQL on stdin (heredoc) whenever you use `-v`.
  ⛔ Neither a grep gate nor `bash -n` can catch this: it is shell-valid and semantically
  dead.
- **Traefik needs a moment after `docker compose up -d`.** `trace.saldivar.io` returned
  404 immediately after recreating tracearr and 200 shortly after. Re-test before
  declaring a regression.

**Never report a gate as passing when its output was empty.**

**Scan for credentials with a broad pattern** (`USER|PASS|KEY|TOKEN|SECRET|CLAIM|AUTH|
CRED` on the variable *name*). A scan for "password" once missed `VPN_PASS` and leaked
PIA credentials into a commit.

**`git add` before `nix build`** — flakes only see tracked files.

**Codex workers check out HEAD**, so commit before asking for review.

---

## Reference

| file | what it holds |
|---|---|
| `K3S-BASELINE-MEDIA.md` | the wave's captured baseline — status codes, DB sizes, health endpoints |
| `K3S-MIGRATION-PLAN.md` | phases and decisions **D1–D15** (D14/D15 matter most; see corrections above) |
| `K3S-MIGRATION-LEDGER.md` | all 35 services, measured resources, per-service hazards |
| `K3S-PLEXNET-INVENTORY.md` | live ground truth for the 12 plex-net services |
| `K3S-PHASE1-PLAN.md` | Phase 1, all 5 gates with evidence |
| `pelargir/ROLLBACK.md` | ⛔ opens with the encryption check — read before any rollback |

**Rollback order matters:** neutralise auto-deploy → delete deployments → delete
`k8s-*.yml` routes → *then* `docker compose --profile migrated up -d <svc>`. The
irreversible failure is a k3s restart resurrecting Pods against docker-owned SQLite.
