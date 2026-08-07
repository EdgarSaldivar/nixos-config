# minas-tirith → k3s migration ledger (P0.10)

Generated **2026-08-06** from the LIVE host — `docker inspect` of all 35 running
containers, registry manifest inspection for architecture, and a scan of each
application's own SQLite store for dependencies that never appear in the environment.

> Regenerate after ANY compose change; this was rebuilt once already because the
> first capture predated the `~`→`/root` path fix and recorded four wrong paths.

> ⚠️ **PARKED SINCE CAPTURE (2026-08-06).** The three `infra` rows — `infra-api-1`,
> `infra-minio-1`, `infra-postgres-1` — are **stopped**, not running. PinCollector is
> parked (see `K3S-MIGRATION-PLAN.md` §0b), so the live count is **32**, not 35, and the
> active migration scope is **two** PostgreSQL clusters and **two** GPU consumers. Rows
> are kept rather than deleted so the inventory stays complete; regenerate this file if
> the stack returns.

**Legend** — `PRIV` privileged · `CAP:` added capabilities · `NETNS` shares another
container's netns (→ one multi-container Pod) · `DEV` host device · `LOCAL-BUILD`
no registry, k3s **cannot pull it** · `AMD64` single-arch, must not land on the ARM
control plane · `(appdb)` dependency found inside the app's own database.

| Service | Proj | Arch | Pinned | Config hostPath | Ports | Ingress | Depends on | Special | ns |
|---|---|---|---|---|---|---|---|---|---|
| `audiobookshelf` | books | amd64+arm64 | **YES** | `/usr/local/etc/audiobookshelf` | - | listen.saldivar.io | - | - | books |
| `flaresolverr-books` | books | amd64+arm64 |  | `-` | - | - | - | **NETNS** | books |
| `gluetun` | books | amd64 |  | `-` | 6881 8080 | books-dl.saldivar.io | - | CAP DEV | books |
| `kavita` | books | amd64+arm64 | **YES** | `/usr/local/etc/kavita` | - | books.saldivar.io | _(appdb)_ calibre plex | - | books |
| `qbittorrent-books` | books | amd64+arm64 | **YES** | `/usr/local/etc/qbittorrent-books` | - | - | - | **NETNS** | books |
| `readmeabook` | books | amd64+arm64 | **YES** | `/usr/local/etc/readmeabook/config` | - | bookrequests.saldivar.io | - | - | books |
| `shelfmark` | books | amd64+arm64 | **YES** | `/home/edgar/git/docker/books/shelfmark/config` | - | requestbooks.saldivar.io | _(appdb)_ prowlarr | - | books |
| `nextcloud` | cloud | amd64+arm64 | **YES** | `-` | - | drive.saldivar.io | nextcloud-db | - | cloud |
| `nextcloud-db` | cloud | amd64+arm64 | **YES** | `-` | - | - | - | - | cloud |
| `nextcloud-redis` | cloud | amd64+arm64 |  | `/usr/local/lib/docker-nextcloud-redis` | - | - | - | - | cloud |
| `palworld-server` | gameservers | amd64+arm64 |  | `-` | 27015 8211 | - | - | - | games |
| `immich` | immich | amd64 | **YES** | `/home/edgar/git/docker/immich/config` | 8080 | immich.saldivar.io | immich-postgres14 immich-redis | **AMD64** | photos |
| `immich-postgres14` | immich | amd64+arm64 | **YES** | `-` | 5432 | - | - | - | photos |
| `immich-redis` | immich | amd64+arm64 |  | `-` | 6379 | - | - | - | photos |
| `infra-api-1` | infra | amd64 |  | `-` | 8000 | admin.pin.saldivar.io,pin.saldivar.io | minio:9000 postgres postgres:5432 | **LOCAL-BUILD** | infra |
| `infra-minio-1` | infra | amd64+arm64 |  | `-` | 9000 9001 | - | - | - | infra |
| `infra-postgres-1` | infra | amd64+arm64 |  | `-` | 5432 | - | - | - | infra |
| `traefik2` | infra | amd64+arm64 |  | `-` | 443 80 | traefik.saldivar.io | - | - | infra |
| `animearr` | media | amd64+arm64 | **YES** | `/storage/Media/Torrents;/home/edgar/docker-services/animearr/config` | 8989 | anime.saldivar.io | - | - | media |
| `calibre` | media | amd64+arm64 | **YES** | `/etc/calibre/config` | 8080 8081 8181 | - | - | - | media |
| `deluge-books` | media | amd64+arm64 | **YES** | `/storage/Media/Torrents;/usr/local/etc/deluge-books` | 48846 48946 58846 58946 8112 8118 | btbooks.saldivar.io | - | **PRIV** | media |
| `deluge-vpn` | media | amd64+arm64 | **YES** | `/storage/Media/Torrents;/usr/local/etc/docker-deluge-vpn` | 58846 58946 8112 8118 | bt.saldivar.io | - | **PRIV** | media |
| `flaresolverr` | media | amd64+arm64 |  | `-` | 8191 | - | - | - | media |
| `jellyfin` | media | amd64+arm64 | **YES** | `/usr/local/etc/jellyfin/config` | 8096 8920 | jellyfin.saldivar.io | - | DEV | media |
| `komga` | media | amd64+arm64 | **YES** | `/etc/komga/config;/storage/Media/manga` | 25600 | komga.saldivar.io | - | - | media |
| `lidarr` | media | amd64+arm64 | **YES** | `/etc/lidarr/data` | 8686 | lidarr.saldivar.io | _(appdb)_ prowlarr deluge-vpn plex | - | media |
| `maintainerr` | media | amd64+arm64 |  | `-` | - | maintainerr.saldivar.io | - | - | media |
| `media-tautulli-1` | media | amd64+arm64 |  | `/usr/local/etc/tautulli` | - | tautulli.saldivar.io | _(appdb)_ plex | - | media |
| `media-tracearr-1` | media | amd64+arm64 |  | `-` | 3000 | trace.saldivar.io | redis | - | media |
| `overseerr` | media | amd64+arm64 |  | `/usr/local/etc/docker-overseer` | 5055 | overseer.saldivar.io,requests.saldivar.io | - | - | media |
| `plex` | media | amd64+arm64 | **YES** | `/home/edgar/docker-services/plex/config` | 32400 | plex.saldivar.io | - | DEV | media |
| `prowlarr` | media | amd64+arm64 |  | `/usr/local/etc/docker-prowlarr` | - | prowlarr.saldivar.io | _(appdb)_ animearr sonarr radarr lidarr readmeabook deluge-books flaresolverr plex | - | media |
| `radarr` | media | amd64+arm64 | **YES** | `/home/edgar/docker-services/radarr/config` | 7878 | radarr.saldivar.io | - | - | media |
| `sonarr` | media | amd64+arm64 | **YES** | `/home/edgar/docker-services/sonarr/config;/storage/Media/Torrents` | 8989 | sonarr.saldivar.io | - | - | media |
| `wrapperr` | media | amd64+arm64 |  | `/etc/wrapper` | 8282 | stats.saldivar.io,wrapperr.saldivar.io | - | - | media |

## What this changes in the plan

- **GPU consumers are 2, not 3.** Only `plex` and `jellyfin` hold the GPU; it is
  idle (1 MiB / 8192 MiB, 0%). `model-service` is not running. `immich` uses a
  `-cuda` image but requests **no GPU at all** — pre-existing, likely unintended.
- **Two local-build images, not one.** `pin-collector-api:local` as well as
  `model-service`. Neither can be pulled by k3s → P0.9 is larger than stated.
- **Architecture is nearly a non-issue.** Only `immich` is single-arch amd64;
  everything else publishes arm64. Data locality, not arch, is the binding
  constraint — 16 services are pinned to minas by `/storage*` mounts.
- **The dependency graph is mostly invisible to env vars.** Only 7 edges come
  from the environment; the `*arr` mesh lives inside application databases.


## A.5 — observed resource usage (owner: Edgar, per service)

> ⚠️ **A SINGLE SAMPLE IS NOT A BASIS FOR REQUESTS.** This is one `docker stats`
> reading taken 2026-08-06, useful for spotting outliers and for a first order of
> magnitude — nothing more. Kubernetes requests should come from **sustained**
> observation (p95 over days, including a Plex transcode and an *arr import run),
> because a request set from an idle sample will be too low exactly when load
> arrives. Fill the Request column from measurement before that service migrates;
> a row without one does not migrate.

| Service | CPU (sample) | Mem (sample) | CPU req | Mem req | Owner |
|---|---:|---:|---|---|---|
| `animearr` | 0.03% | 188.4MiB | _tbd_ | _tbd_ | Edgar |
| `audiobookshelf` | 0.01% | 59.55MiB | _tbd_ | _tbd_ | Edgar |
| `calibre` | 0.66% | 278MiB | _tbd_ | _tbd_ | Edgar |
| `deluge-books` | 1.93% | 49.4MiB | _tbd_ | _tbd_ | Edgar |
| `deluge-vpn` | 3.53% | 24.93GiB | _tbd_ | _tbd_ | Edgar |
| `flaresolverr` | 0.00% | 157.6MiB | _tbd_ | _tbd_ | Edgar |
| `flaresolverr-books` | 0.00% | 38.45MiB | _tbd_ | _tbd_ | Edgar |
| `gluetun` | 0.01% | 45.92MiB | _tbd_ | _tbd_ | Edgar |
| `immich` | 0.19% | 726.5MiB | _tbd_ | _tbd_ | Edgar |
| `immich-postgres14` | 0.00% | 84.96MiB | _tbd_ | _tbd_ | Edgar |
| `immich-redis` | 0.14% | 9.547MiB | _tbd_ | _tbd_ | Edgar |
| `jellyfin` | 0.00% | 742.4MiB | _tbd_ | _tbd_ | Edgar |
| `kavita` | 0.25% | 188.9MiB | _tbd_ | _tbd_ | Edgar |
| `komga` | 0.07% | 785.9MiB | _tbd_ | _tbd_ | Edgar |
| `lidarr` | 0.03% | 149.2MiB | _tbd_ | _tbd_ | Edgar |
| `maintainerr` | 0.00% | 148.7MiB | _tbd_ | _tbd_ | Edgar |
| `media-tautulli-1` | 0.01% | 110.9MiB | _tbd_ | _tbd_ | Edgar |
| `media-tracearr-1` | 0.08% | 1.353GiB | _tbd_ | _tbd_ | Edgar |
| `nextcloud` | 0.01% | 152.1MiB | _tbd_ | _tbd_ | Edgar |
| `nextcloud-db` | 0.00% | 101.7MiB | _tbd_ | _tbd_ | Edgar |
| `nextcloud-redis` | 0.04% | 3.281MiB | _tbd_ | _tbd_ | Edgar |
| `overseerr` | 0.01% | 168MiB | _tbd_ | _tbd_ | Edgar |
| `palworld-server` | 0.00% | 18.55MiB | _tbd_ | _tbd_ | Edgar |
| `plex` | 0.12% | 292.3MiB | _tbd_ | _tbd_ | Edgar |
| `prowlarr` | 0.03% | 215MiB | _tbd_ | _tbd_ | Edgar |
| `qbittorrent-books` | 0.01% | 30.04MiB | _tbd_ | _tbd_ | Edgar |
| `radarr` | 0.03% | 215.1MiB | _tbd_ | _tbd_ | Edgar |
| `readmeabook` | 0.06% | 278.4MiB | _tbd_ | _tbd_ | Edgar |
| `shelfmark` | 0.02% | 109.6MiB | _tbd_ | _tbd_ | Edgar |
| `sonarr` | 0.04% | 190.9MiB | _tbd_ | _tbd_ | Edgar |
| `traefik2` | 0.00% | 34.43MiB | _tbd_ | _tbd_ | Edgar |
| `wrapperr` | 0.00% | 3.5MiB | _tbd_ | _tbd_ | Edgar |

**Outliers to investigate before setting any request:**
- **`deluge-vpn` — 24.93 GiB is PAGE CACHE, not a leak. DIAGNOSED 2026-08-06.**
  Measured inside the container's cgroup:

  | | |
  |---|---|
  | `file` (page cache) | **26 GB** ← the entire figure |
  | `anon` (truly private) | 197 MB |
  | `sock` (connections) | **0.4 MB** |
  | established connections | 41 |
  | `deluged` RSS | 2.7 GB (includes file-backed pages) |

  So it is torrent file I/O being cached by the kernel and accounted to the cgroup —
  **not** connection buffers (the initial hypothesis; socket memory is negligible at
  41 connections) and not a leak.

  **CORRECTION — an earlier revision of this section was wrong and would have caused
  evictions.** It claimed Kubernetes' working set (`memory.current − inactive_file`)
  largely excludes this cache, so it could be ignored. Measured, deluge's
  `inactive_file` is only ~315 MB, giving a **working set of 29.7 GB**. The cache is
  *active*, not inactive, so kubelet's eviction signal counts nearly all of it. Page
  cache being reclaimable by the kernel does **not** make it invisible to kubelet.

  **What to actually do — and it is a memory LIMIT, not a request:**
  - **Request** from `anon` (~200 MB): that is the memory that genuinely cannot be
    reclaimed and so is all the scheduler must reserve.
  - **Limit** (~4 GiB) is the load-bearing setting. It caps the cgroup, which forces
    page-cache reclaim *within* the cgroup, so the working set stays bounded and
    kubelet never sees 29 GB. With only ~200 MB anon there is no OOM-kill risk — the
    kernel evicts cache long before it reaches the anonymous pages.

  Without a limit this Pod reports a ~30 GB working set on a node with finite RAM and
  becomes the first thing kubelet evicts under pressure — while being, in truth, a
  200 MB process.
- **`palworld-server` — 107.89% CPU**, over a full core while nominally idle. PC1 later
  found it has **611 restarts** — it is crash-looping, not merely busy.
- `media-tracearr-1` 1.36 GiB and `komga` 786 MiB are the next largest and look
  plausible for a JVM and a Node app respectively.

### A.5 — MEASURED working sets (2026-08-06). Use these, not the `docker stats` column.

Read from each container's cgroup: `anon` is memory that cannot be reclaimed, `cache`
is page cache, `WS` is what kubelet actually measures (`memory.current − inactive_file`).

| container | docker says | anon | cache | **WS (kubelet sees)** |
|---|---|---|---|---|
| deluge-vpn | 30031M | 194M | 29742M | **29716M** ⚠️ needs a limit |
| media-tracearr-1 | 1414M | 260M | 1110M | **1388M** |
| jellyfin | 1162M | 769M | 366M | **870M** |
| komga | 810M | 778M | 25M | **785M** |
| immich | 760M | 666M | 56M | **733M** |
| plex | 2060M | 251M | 1762M | **353M** |
| readmeabook | 305M | 194M | 79M | **279M** |
| calibre | 295M | 242M | 31M | **276M** |
| prowlarr | 259M | 102M | 149M | **213M** |
| radarr | 318M | 127M | 183M | **211M** |
| kavita | 235M | 108M | 121M | **191M** |
| animearr | 265M | 170M | 90M | **188M** |
| sonarr | 971M | 168M | 789M | **183M** |
| overseerr | 187M | 139M | 33M | **167M** |
| flaresolverr | 463M | 141M | 315M | **157M** |
| nextcloud | 167M | 54M | 69M | **151M** |
| maintainerr | 179M | 123M | 41M | **149M** |
| lidarr | 182M | 69M | 107M | **149M** |
| media-tautulli-1 | 227M | 78M | 121M | **112M** |
| shelfmark | 128M | 100M | 24M | **109M** |
| nextcloud-db | 104M | 4M | 97M | **101M** |
| immich-postgres14 | 108M | 7M | 96M | **85M** |
| audiobookshelf | 63M | 42M | 16M | **60M** |
| deluge-books | 53M | 27M | 17M | **48M** |
| gluetun | 56M | 38M | 16M | **46M** |
| flaresolverr-books | 52M | 35M | 13M | **38M** |
| traefik2 | 38M | 27M | 9M | **32M** |
| qbittorrent-books | 33M | 14M | 15M | **29M** |
| palworld-server | 29M | 22M | 2M | **29M** |
| immich-redis | 22M | 6M | 15M | **9M** |
| wrapperr | 8M | 2M | 5M | **3M** |
| nextcloud-redis | 4M | 2M | 1M | **3M** |

**The headline number: every service except deluge-vpn totals ~7 GB of working set.**
Not the ~40 GB the `docker stats` column implies. On a 125 GB host the whole fleet is
comfortable — the scheduling problem is one outlier, not 32 fat services.

**How much `docker stats` misleads, per service:** plex reports 2060M and is 353M
(5.8× over); sonarr reports 971M and is 183M (5.3×); flaresolverr 463M → 157M. Sizing
requests from the reported column would have over-reserved several-fold across the
fleet and made a 125 GB node look full at a fraction of its capacity.

**Rules this produces:**
1. Request ≈ `anon` rounded up. It is the only genuinely unreclaimable part.
2. Limit ≈ 2–3× WS for normal services; for the I/O-heavy ones (deluge-vpn, plex,
   sonarr, flaresolverr) set a limit deliberately to bound page-cache growth.
3. Never copy the `docker stats` column into a manifest.

---

## ⛔ Services that must NOT be transliterated — read before writing their manifests

Found by the PC1 baseline (2026-08-06). All three are broken *today*, in docker, before
any migration. Recorded here because the ledger is what gets consulted while writing a
manifest, and each of these produces a failure that looks exactly like migration damage.

### `nextcloud-redis` — health check is wrong, service is fine
Reports unhealthy with 0 restarts and 5h uptime. Its check sends `-a <password>` to a
redis with **no password configured**, so it fails with
`AUTH failed: called without any password configured`.

Docker tolerates an unhealthy container — it keeps running and keeps serving, which is
why nobody noticed. **Kubernetes will not.** Copy that check into a `readinessProbe` and
the Pod never becomes Ready: no traffic is routed, the Deployment never finishes rolling
out, and nextcloud appears to have been broken by the migration. **Drop the `-a`
argument, or configure a password. Do not port it verbatim.**

### `deluge-books` — health check exceeds its own timeout
Unhealthy, 0 restarts, up 5h; the check repeatedly logs `Health check exceeded timeout
(30s)`. Same class of problem: the application is up, the probe is not viable. Give the
Kubernetes probe a realistic `timeoutSeconds`/`periodSeconds` and `initialDelaySeconds`,
or omit the readiness probe rather than import a check that cannot pass.

### `palworld-server` — was crash-looping; **FIXED 2026-08-06**, now migratable
It had restarted **753** times over eight days. Cause, stated plainly in its own log:
`Error: Save data is corrupted. Please restore from a backup`, then `LowLevelFatalError`
and SIGSEGV. `Level.sav` was **zero bytes**, written **2026-07-29 21:32** — the
read-only filesystem failure recorded in `monitoring.nix`. The save write landed exactly
as the filesystem went read-only and truncated the world.

Restored from the container's own Jul 29 00:00 backup (`Level.sav` 79,539 bytes,
gzip-verified). The corrupt save was moved to `...corrupt-2026-08-06`, not deleted.
Now: `restarts=0`, `health=healthy`, CPU 107% -> 13%, memory 29 MiB -> **959 MiB** — the
memory being the real proof, since it had never previously loaded the world.

**Migration note:** its resource row is now meaningless. The 29 MiB / 107% CPU figures
were a *crashing* process; a loaded world is ~1 GB and 13%. Re-measure before sizing.

⚠️ **Its auto-update runs hourly and re-verifies 5.15 GB via steamcmd.** While looping
that meant roughly **700 GB/hour** of disk reads. Even healthy, an hourly full verify is
worth reconsidering — and in Kubernetes it becomes an init-container pattern, not a
cron inside the pod.

⚠️ **Backup retention is unbounded**: `DELETE_OLD_BACKUPS=false` with 232 tarballs going
back to 2024-01-31 — **29 GB of the 34 GB** the service occupies, mostly ~321 MB files
from the 2024 world. Setting `DELETE_OLD_BACKUPS=true` would honour the existing
`OLD_BACKUP_DAYS=30` and delete ~2.5 years of save history. That is a data-retention
decision for the owner, not a cleanup to perform quietly.

### The general rule this establishes
Compose `healthcheck:` blocks are advisory in docker and **load-bearing** in Kubernetes.
Every one of the 35 services needs its check reviewed as a *probe* before translation,
not copied. A probe that cannot pass converts a working service into an unreachable one,
and does so silently — the Pod is Running, merely never Ready.
