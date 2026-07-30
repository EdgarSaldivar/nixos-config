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
> containers as orphans and will delete them. Fix it properly during restore by giving
> one an explicit `name:` (or `COMPOSE_PROJECT_NAME`).
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

```bash
sudo systemctl stop docker.socket docker.service
B=/storage2/backup-2026-07-30

# Service state living OUTSIDE /etc — safe to restore wholesale
sudo rsync -aHAX --info=stats2 "$B/local/"       /usr/local/
sudo rsync -aHAX --info=stats2 "$B/opt/"         /opt/
sudo rsync -aHAX --info=stats2 "$B/home/edgar/"  /home/edgar/
sudo rsync -aHAX --info=stats2 "$B/volumes/"     /var/lib/docker/volumes/

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

## Afterwards

- Point the nightly `backup-root-data` timer at the restored paths and confirm it runs.
- Consider moving hot databases off the NVMe — Jellyfin alone wrote 1.29 GB/h and was the file
  that found a bad block. Deferred by choice (root is faster), so the nightly ZFS backup is the
  compensating control.
- Add an OnCalendar healthchecks check for the backup timer, so silent backup failure is visible.
