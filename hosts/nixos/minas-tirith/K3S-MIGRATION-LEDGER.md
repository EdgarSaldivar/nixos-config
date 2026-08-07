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
