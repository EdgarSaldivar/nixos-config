# minas-tirith — service restore runbook

Bringing 39 containers across 6 compose projects back after the NixOS install.
Run **after** `INSTALL-RUNBOOK.md` step 8 (pools imported and verified).

> **This repo is PUBLIC.** Secrets (BMC credentials, LUKS passphrase, the
> healthchecks URL, `.env` files) are never committed — see the note at the top of
> `INSTALL-RUNBOOK.md` for what is deliberately committed and why.
>
> The compose tree restored here **does** contain credentials. Its first commit
> message is literally `🚨 DON'T EVER PUSH THIS TO GITHUB 🚨`. Keep it that way.


---

## What we're restoring

Observed live on 2026-07-30 immediately before shutdown — 37 running, 39 container
objects, 49 named volumes. This supersedes an earlier table in this file that was
counted from compose *files*: it listed a `services` project that does not exist and
omitted `gameservers` entirely.

| Project | Running | Directory |
|---|---:|---|
| `media` | 18 | `~/git/docker/media` |
| `infra` | 6 | **two dirs — see collision below** |
| `books` | 6 | `~/git/docker/books` |
| `immich` | 3 | `~/git/docker/immich` |
| `cloud` | 3 | `~/git/docker/cloud` |
| `gameservers` | 1 | `~/git/gameservers` ← **not under `~/git/docker/`** |

> ### ⚠️ `infra` is TWO different stacks sharing one project name
> `~/git/docker/infra` → `traefik2`, `infra-host-hostnames-1`
> `~/PinCollector/infra` → `infra-minio-1`, `infra-api-1`, `infra-model-service-1`, `infra-postgres-1`
>
> Both default to compose project `infra` and share the `infra_default` network. On
> shutdown this was not academic: `infra-model-service-1` survived `compose down` in
> *both* directories and had to be removed by hand, and `infra_default` refused to go
> away with *"Resource is still in use"*.
>
> **Never pass `--remove-orphans` in either directory** — each stack sees the other's
> containers as orphans and will delete them.
>
> ⚠️ **Do NOT rename the project during the restore.** Setting
> `COMPOSE_PROJECT_NAME=pincollector` also renames every unpinned resource:
> `infra_default` becomes `pincollector_default`, and `infra_pin_collector_pgdata`
> becomes `pincollector_pin_collector_pgdata` — i.e. PinCollector starts against
> **brand-new empty volumes** while its real data sits in the old ones. Restore with
> the name as-is; if you want to fix the collision, do it afterwards and pin the
> existing network and every stateful volume by explicit `name:` first, comparing
> `docker compose config` before and after.
>
> Note also that `infra-model-service-1` is the Triton/GPU service referenced in the GPU
> section below — it belongs to **PinCollector**, not to `~/git/docker/infra`.

Data lives in three places, all captured:

| What | Where | Size |
|---|---|---|
| Bind-mount configs | `/etc`, `/usr/local/etc`, `/home/edgar/docker-services`, `/opt`, `/srv` | ~429 GB |
| Named volumes | `/var/lib/docker/volumes` (49 volumes) | ~2 GB |
| Compose files + env | `/home/edgar/git/docker` | ~1 GB |
| Bulk media | ZFS pools — **never moved, never at risk** | ~98 TB |

Backup source: `/storage2/backup-2026-07-30/`

---

## ⚠️ Ordering hazards — read before starting

**1. Pools must be imported first.** Four of six projects bind-mount ZFS paths. Starting compose
before the import makes Docker helpfully create *empty directories* at those mountpoints, which
then blocks the ZFS mount. Recovering means stopping everything, removing the stray dirs, and
re-importing.

**2. Nine external networks must exist before `compose up`:**
```
traefik-net  books-net  logger-net  nextcloud-net  plex-net  s3-net
immich_default  infra_default  gameservers_default
```
`*_default` names are compose-generated and will be recreated automatically; the rest are declared
`external: true` and must be made by hand or compose fails immediately.

**3. `.env` files are NOT in git.** Found outside version control:
```
docker/authentik.env   docker/traefik.env   docker/output.env
docker/books/.env      docker/infra/traefik.env
```
They ARE in the backup. Without them, traefik and several services start with empty config and
silently misbehave. The repo's first commit message is literally
`🚨 DON'T EVER PUSH THIS TO GITHUB 🚨` — treat this tree as containing credentials.

**4. Docker's default runtime changed.** The old daemon set `default-runtime: nvidia`, so *every*
container got the nvidia runtime. `containers.nix` sets it back to `runc` — deliberately, so a
broken nvidia toolkit can't take down all 39 services. **The GPU container must now request the
GPU explicitly.** Expect exactly one service to need editing (the Triton `model-service`), plus
Plex/Jellyfin if they use hardware transcode.

**5. Address pool.** `containers.nix` pins `172.16.0.0/12`. Verify with `docker network inspect`
that nothing landed on `192.168.x` — that collision blackholes WireGuard return traffic.

---

## Restore

### 1. Data (pools imported, docker stopped)

> ### 🚫 NEVER blanket-restore `/etc`
>
> NixOS manages `/etc` declaratively — most of it is symlinks into the Nix store.
> `rsync "$B/etc/" /etc/` overwrites those with openSUSE's files and **breaks the
> system**, including `/etc/ssh` (which sops depends on) and `/etc/passwd`.
> There is deliberately no such command anywhere in this runbook. Restore only
> the named service directories below.

> **Enter bash first.** `ssh minas` lands you in **fish**, and the very next line
> (`B=...`) is bash-only assignment. In fish it fails, `$B` is then empty, and the
> rsync commands below run against `/` — the single most likely way this restore
> goes wrong. Stay as `edgar` (the runbook uses `~`), do not `sudo -i`:
>
> ```bash
> ssh -t minas 'exec env BASH_NO_FISH=1 bash -il'
> [ -n "$BASH_VERSION" ] || { echo "NOT BASH — STOP"; }
> ```

```bash
set -euo pipefail

# Stop the scheduled jobs for the duration of the restore. Both will otherwise
# fire mid-restore on a half-populated filesystem:
#   - backup-root-data would snapshot a PARTIAL restore and stamp it a success,
#     and (running before any container exists) would find no databases to dump
#     while clearing the degraded marker.
#   - the first monthly autoscrub lands Aug 1 00:00 +0-6h, i.e. potentially while
#     298 GB is being written back, across both ~98 TB pools.
sudo systemctl stop  backup-root-data.timer zfs-scrub.timer 2>/dev/null || true
sudo systemctl stop docker.socket docker.service
B=/storage2/backup-2026-07-30
[ -d "$B" ] || { echo "BACKUP MISSING — STOP"; exit 1; }

# Service state living OUTSIDE /etc — safe to restore wholesale
sudo rsync -aHAX --info=stats2 "$B/local/"       /usr/local/
sudo rsync -aHAX --info=stats2 "$B/opt/"         /opt/          # 2.0 GB
# ⚠️ EXCLUDE home-manager-managed dotfiles. HM owns ~/.config/fish, ~/.bashrc,
# ~/.bash_profile and ~/.config/starship.toml as SYMLINKS into the Nix store.
# Restoring the old regular files over them makes the next activation abort on a
# file-collision, and `nixos-rebuild switch` then fails until repaired by hand —
# on a box where a failed rebuild is how you lose an afternoon.
sudo rsync -aHAX --info=stats2 \
  --exclude='.config/fish/***' --exclude='.bashrc' --exclude='.bash_profile' \
  --exclude='.profile' --exclude='.config/starship.toml' --exclude='.zshrc' \
  "$B/home/edgar/"  /home/edgar/
# The old fish/tide config remains in the backup if you ever want to port it into
# users/edgar/home.nix — restore it declaratively, not by copying files back.
sudo rsync -aHAX --info=stats2 "$B/volumes/"     /var/lib/docker/volumes/

# /srv is deliberately NOT restored. It is captured by the backup, but measured
# 0 bytes on the live host — only empty `svn/` and `tftpboot/` directories left
# over from openSUSE defaults, which NixOS does not use. Verify before skipping:
#   du -sh "$B/srv"    # expect ~1.5K of empty dirs, not service data

# Service state that happens to live under /etc — NAMED DIRECTORIES ONLY
sudo rsync -aHAX "$B/etc/gameservers/"  /etc/gameservers/     # palworld saves (~60GB)
sudo rsync -aHAX "$B/etc/calibre/"      /etc/calibre/
sudo rsync -aHAX "$B/etc/komga/"        /etc/komga/
sudo rsync -aHAX "$B/etc/lidarr/"       /etc/lidarr/
sudo rsync -aHAX "$B/etc/readarr/"      /etc/readarr/
sudo rsync -aHAX "$B/etc/wrapper/"      /etc/wrapper/
sudo rsync -aHAX "$B/etc/letsencrypt/"  /etc/letsencrypt/
```

Ownership matters — several services run as uid 911 (Jellyfin) or 1000. `-aHAX` preserves it;
verify afterwards:
```bash
sudo ls -ln /usr/local/etc/jellyfin/config/data/data/library.db   # expect 911:911
```

> ### ⛔ `~` IN A COMPOSE FILE IS NOT `/home/edgar` — IT IS `/root`
>
> **This bit on 2026-08-06 and cost four services three hours of running on blank
> configuration.** It is invisible to every "is it up?" check.
>
> `media/docker-compose.yaml` bound four services with a tilde:
>
> ```yaml
>   - ~/docker-services/plex/config:/config      # and sonarr, radarr, animearr
> ```
>
> Compose expands `~` from **`$HOME` of the process**, and every command in this
> runbook is `sudo docker compose`, where `HOME=/root`. So the bind resolved to
> `/root/docker-services/...`, which the restore never populates — the backup has
> no `root/` at all (top level is `etc home local opt plex srv volumes`).
>
> Result: plex, sonarr, radarr and animearr each **created a fresh empty config**
> and started as though newly installed. Every container reported `Up`. Traefik
> routed to them. The GPU worked. Only an application-level query exposed it:
>
> ```
> /home/edgar/docker-services/sonarr/config/sonarr.db   clients=1 indexers=26 series=812
> /root/docker-services/sonarr/config/sonarr.db         clients=0 indexers=0  series=0
> ```
>
> Nothing was lost — the real configs (including **330 GB** of Plex) sat untouched
> at `/home/edgar/docker-services/`. But Plex would have begun rebuilding a library
> from scratch, and any write the blank instances made would have diverged.
>
> **Fixed by making the paths absolute**, which also removes the ambiguity for the
> k3s migration (hostPath needs absolute paths regardless):
>
> ```yaml
>   - /home/edgar/docker-services/plex/config:/config
> ```
>
> **Check before starting any stack:**
>
> ```bash
> grep -rn '^\s*-\s*~/' ~/git/docker/*/docker-compose.y*ml ~/git/gameservers/*.y*ml
> #   any hit is a path that will resolve under /root when run with sudo
> sudo sh -c 'echo $HOME'      # confirm what ~ will actually become
> ```
>
> **And verify at the APPLICATION layer, not the container layer** — see the
> functional smoke tests below. "Running", a 200 through traefik, and a working GPU
> were all true while the library was empty.

### 2. Networks

```bash
sudo systemctl start docker
for n in traefik-net books-net logger-net nextcloud-net plex-net s3-net; do
  sudo docker network create "$n" 2>/dev/null || echo "$n exists"
done
```

### 3. Bring stacks up — one at a time, verify between

Order matters: `infra` carries traefik, which everything else routes through.

```bash
cd ~/git/docker/infra  && sudo docker compose up -d && sleep 20   # traefik first
cd ~/git/docker/media  && sudo docker compose up -d && sleep 30
cd ~/git/docker/cloud  && sudo docker compose up -d && sleep 20
cd ~/git/docker/books  && sudo docker compose up -d && sleep 20
cd ~/git/docker/immich && sudo docker compose up -d && sleep 20
cd ~/git/gameservers   && sudo docker compose up -d && sleep 20   # palworld, ~60GB saves
cd ~/PinCollector/infra && sudo docker compose up -d              # collides on `infra` — fix name first
```

**Do not bring them all up at once.** 39 containers starting together on a fresh install means
any failure is buried in noise, and several carry databases that will attempt migrations.

### 4. GPU

```bash
nvidia-smi                                              # driver loaded (595.x on 26.05)
sudo docker run --rm --gpus all <cached-cuda-image> nvidia-smi
```
Then edit the Triton service to request the GPU explicitly, since the default runtime is now runc:
```yaml
    deploy:
      resources:
        reservations:
          devices: [{ driver: nvidia, count: 1, capabilities: [gpu] }]
```

---

## Verification

```bash
sudo docker ps --format '{{.Names}}\t{{.Status}}' | sort        # expect ~39
sudo docker ps --filter health=unhealthy                       # nextcloud-redis, deluge-books
                                                               # were unhealthy BEFORE — not new
sudo docker network inspect traefik-net | grep Subnet          # 172.16.0.0/12, NOT 192.168.x
```

### ⛔ If a restored PostgreSQL will not start — use the COLD copies

The main backup's postgres directories were captured while the containers were
**running**, so they are hot copies and are not guaranteed to start. Cold copies of
every cluster were taken on 2026-07-30 with all containers stopped:

```bash
cat /storage2/backup-2026-07-30-pgcold/README.txt     # read this first
ls  /storage2/backup-2026-07-30-pgcold/
```

Covers grafana-db, nextcloud-db, tracearr_postgres, rmab-pgdata, inventree-pgdb and
two hash-named volumes. **PinCollector is deliberately absent** — its live volume was
empty, so restore that one from the main backup at
`volumes/infra_pin_collector_pgdata/_data` (13 MB, PG 16, captured clean).

To use one: stop the stack, replace the volume contents, chown to the uid the
container expects, start ONLY postgres, confirm it accepts connections, then start
dependents.

Note the nightly `pg_dumpall` dumps do **not** help for this migration — the first
backup runs before any container exists, so it discovers no databases and writes no
dumps. Those dumps only protect you going forward.

### Restoring a PostgreSQL database from the nightly dumps

The nightly backup writes `pg_dumpall` output to `/storage2/backup/dumps/`, and
**these are the copies to trust** — the rsync'd `/var/lib/docker/volumes` copy of a
running Postgres cluster may refuse to start, because a hot file copy of a live
data directory is not a valid backup. ZFS snapshots do not change that: they
preserve whatever bytes arrived, consistent or not.

```bash
# Set these two, then the rest is copy-paste. (Placeholders like <container>
# are shell redirects and fail with a confusing syntax error — use variables.)
CONTAINER=immich-postgres                       # <-- the postgres container
PGUSER=$(sudo docker exec "$CONTAINER" printenv POSTGRES_USER 2>/dev/null || echo postgres)

ls -lh /storage2/backup/dumps/                  # one .sql.gz per postgres container
zcat "/storage2/backup/dumps/$CONTAINER.sql.gz" | head -20   # sanity-check it

# restore into a running, EMPTY cluster. ON_ERROR_STOP is what makes a failed
# statement actually fail the command instead of being skipped silently.
set -o pipefail
zcat "/storage2/backup/dumps/$CONTAINER.sql.gz" \
  | sudo docker exec -i "$CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$PGUSER"
```

**Test this at least once, before you need it.** A dump that has never been
restored is a hypothesis, not a backup. Restoring into a throwaway container is
enough to prove the file is valid:

```bash
# Use the SAME image as the source container — immich needs pgvector, and a
# vanilla postgres:16 will fail on the extension while still creating the
# database, which makes a broken restore look successful.
IMG=$(sudo docker inspect "$CONTAINER" -f '{{.Config.Image}}')
sudo docker run --rm -d --name pgtest -e POSTGRES_PASSWORD=x "$IMG"
until sudo docker exec pgtest pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done

# -X ignores ~/.psqlrc; ON_ERROR_STOP=1 is what makes psql EXIT NON-ZERO on a
# failed statement. Without it psql reports success having skipped every broken
# line, and `\l` then "proves" a restore that did not happen.
set -o pipefail
zcat "/storage2/backup/dumps/$CONTAINER.sql.gz" \
  | sudo docker exec -i pgtest psql -X -v ON_ERROR_STOP=1 -U postgres
echo "restore exit: $?"        # MUST be 0

# Verify CONTENT, not just database names:
sudo docker exec pgtest psql -X -U postgres -c '\l'
DBNAME=immich                                   # <-- pick one from the list above
sudo docker exec pgtest psql -X -U postgres -d "$DBNAME" \
  -c "SELECT schemaname,relname,n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 10;"
#   expect real tables with plausible row counts — an empty list means the dump
#   restored a shell with no data.
sudo docker rm -f pgtest
```

If the heartbeat ever reports `DEGRADED dumps:`, the file copy still ran but at
least one database's consistent copy is **stale** — fix it before relying on it.

Databases — the ones that mattered enough to salvage:
```bash
sudo sqlite3 /usr/local/etc/jellyfin/config/data/data/library.db "PRAGMA integrity_check;"
# expect: ok, 103969 items in TypedBaseItems
```
Plex's DB needs Plex's own SQLite build (custom collations) — stock `sqlite3` reports
`unknown tokenizer: collating`, which is a tooling limit, **not** corruption.

Functional smoke tests, not just "container is up":
- Plex + Jellyfin: play something, confirm a transcode
- Immich / Nextcloud: log in, load a photo
- \*arr: web UI loads with library intact
- Triton: an inference request

---

## Known state carried over

- `nextcloud-redis` and `deluge-books` reported unhealthy **before** the migration — pre-existing.
- `audiobookshelf` was recreated on 2026-07-29 to clear a corrupt layer; its config is in the backup.
- Jellyfin's `library.db` in the backup is the **recovered** copy (`sqlite3 .recover`, verified
  `integrity_check ok`, all 103,969 items and 313,963 media streams intact).
- Plex's `blobs.db-wal` was deleted during recovery; Plex regenerates it.
- The Plex backup DB `com.plexapp.plugins.library.db-2026-07-30` was corrupt and removed; the
  07-24 and 07-27 backups remain.

---

## ⛔ Prove the alerting works — only possible HERE, not during the install

The install runbook could only prove the ping *leaves the box*: on a fresh system
there is no backup stamp and no containers, so the heartbeat legitimately reports
UNHEALTHY and an up→down→up test is impossible. Now that the stacks are restored
and a backup has run, the check should finally be **green** — which is the
precondition for testing a real transition.

```bash
# 0. baseline: must be genuinely healthy first, or the rest proves nothing
sudo systemctl start healthcheck-ping.service
sudo journalctl -u healthcheck-ping -n 20 --no-pager     # expect no "problems"
#    confirm the check is UP on healthchecks.io before continuing

# 1. force a CRITICAL event and confirm you actually RECEIVE the alert
sudo touch /var/lib/healthcheck-ping/mce.latched
sudo systemctl start healthcheck-ping.service
#    -> alert must arrive (Telegram/email). If it does not, monitoring is decorative.

# 2. clear it and confirm the RECOVERY notification arrives
sudo rm /var/lib/healthcheck-ping/mce.latched
sudo systemctl start healthcheck-ping.service

# 3. only now is the timer trustworthy
systemctl status healthcheck-ping.timer
```

**Optional but recommended — a dedicated critical check.** The heartbeat supports a
second Healthchecks URL on **line 2** of the sops secret. Without it, machine checks
and disk degradation share the aggregate check; once that is red for any softer
reason (a failed backup, a stopped container) a *new* hardware fault may raise no
new notification, because Healthchecks alerts on state transitions. Create a second
check and append its URL:

```bash
sops secrets/minas-tirith.yaml     # healthchecks-url: line1=aggregate, line2=critical
```

## Re-enable scheduled maintenance — AFTER services are verified

Deliberately last: these were stopped during the restore.

```bash
sudo systemctl start backup-root-data.timer
sudo systemctl start backup-root-data.service   # first REAL backup, with DBs present
journalctl -u backup-root-data -n 40 --no-pager # expect dumps, no "DEGRADED"
ls -lh /storage2/backup/dumps/                  # one .sql.gz per postgres container
zfs list -t snapshot -r storage2/backup         # expect a daily- snapshot

sudo systemctl start zfs-scrub.timer
# Run the first scrub ATTENDED rather than letting the timer surprise you:
#   sudo zpool scrub storage   # ~98 TB, hours. Not during a restore.
```

## Afterwards

- Point the nightly `backup-root-data` timer at the restored paths and confirm it runs.
- Consider moving hot databases off the NVMe — Jellyfin alone wrote 1.29 GB/h and was the file
  that found a bad block. Deferred by choice (root is faster), so the nightly ZFS backup is the
  compensating control.
- Add an OnCalendar healthchecks check for the backup timer, so silent backup failure is visible.

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
```
