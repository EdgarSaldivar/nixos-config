# media wave — PRE-CUTOVER BASELINE (captured 2026-08-07, all services on docker)

Post-cutover verification must match these EXACTLY. Do not verify against "200" —
six of these twelve hostnames do not return 200 when healthy.

## Ingress status, through docker traefik2 on minas

| hostname | status | note |
|---|---|---|
| tautulli.saldivar.io | **303** | redirect to login |
| requests.saldivar.io | **307** | overseerr |
| overseer.saldivar.io | **307** | overseerr, second name |
| prowlarr.saldivar.io | **302** | |
| sonarr.saldivar.io | **401** | auth enabled |
| radarr.saldivar.io | **302** | |
| lidarr.saldivar.io | **200** | |
| anime.saldivar.io | **401** | auth enabled |
| maintainerr.saldivar.io | **401** | basic-auth@file middleware — a 200 here means the middleware was DROPPED |
| stats.saldivar.io | **200** | wrapperr |
| wrapperr.saldivar.io | **200** | wrapperr, second name |
| requestbooks.saldivar.io | **200** | shelfmark |

Command:
```sh
ssh minas 'curl -sk -o /dev/null -w "%{http_code}\n" -H "Host: <name>" https://127.0.0.1/'
```

## Health endpoints (verified 200 / exit 0 unauthenticated, inside each container)

`prowlarr|sonarr|radarr|lidarr|animearr` → `/ping` · `tautulli` → `/status` ·
`overseerr` → `/api/v1/status` · `shelfmark` → `/api/health` · `wrapperr` → `/` ·
`maintainerr` → exec `/opt/app/healthcheck.sh`

## Content anchors — config tree bytes (`du -sb`)

| path | bytes |
|---|---|
| /usr/local/etc/tautulli | 1853174479 |
| /usr/local/etc/docker-overseer | 11060717 |
| /usr/local/etc/docker-prowlarr | 175434469 |
| /home/edgar/docker-services/sonarr/config | 2391914595 |
| /home/edgar/docker-services/radarr/config | 2540157378 |
| /etc/lidarr/data | 106671467 |
| /home/edgar/docker-services/animearr/config | 267412141 |
| /usr/local/etc/maintainerr | 160145 |
| /etc/wrapper | 1217851 |
| /home/edgar/git/docker/books/shelfmark/config | 14788002 |

## Primary databases — byte size (identity check after cutover)

| db | bytes |
|---|---|
| /usr/local/etc/tautulli/tautulli.db | 195973120 |
| /usr/local/etc/docker-overseer/db/db.sqlite3 | 1044480 |
| /usr/local/etc/docker-prowlarr/prowlarr.db | 73195520 |
| /home/edgar/docker-services/sonarr/config/sonarr.db | 960589824 |
| /home/edgar/docker-services/radarr/config/radarr.db | 103411712 |
| /etc/lidarr/data/lidarr.db | 1765376 |
| /home/edgar/docker-services/animearr/config/sonarr.db | 79437824 |
| /usr/local/etc/maintainerr/maintainerr.sqlite | 143360 |
| /home/edgar/git/docker/books/shelfmark/config/users.db | 81920 |

⚠️ `overseerr` has a **4.1 MB `-wal`** open right now. After `docker stop` that file
must be GONE — its absence is what proves a clean checkpoint. `db.sqlite3` will grow when
the WAL is folded in, so its post-stop size legitimately differs from the number above;
compare AFTER the stop, and use that post-stop size as the identity anchor for the Pod.

`wrapperr` has no database (config JSON only).

---

## POST-CUTOVER (2026-08-07) — all fifteen verified identical

Every hostname above returned its baseline code after the wave landed, plus
`listen`/`komga` at 200 and `trace.saldivar.io` at 200. 11 pods Running in `media`,
**0 restarts**. Docker went 29 → 19 containers.

Bare-name resolution verified from inside the namespace — this is what the atomic
cutover existed to preserve, so it is the real acceptance test:

| from | to | result |
|---|---|---|
| sonarr | `http://prowlarr:9696/ping` | 200 |
| maintainerr | `http://tautulli:8181` | 200 |
| maintainerr | `http://overseer:5055` | 200 |
| shelfmark | `http://prowlarr:9696/ping` | 200 |
| shelfmark | `http://flaresolverr:8191` (bridge) | 200 |
| prowlarr | `http://deluge-books:8112` (bridge) | 200 |
| tautulli | `http://plex:32400` (bridge) | 200 |

`hostPort` verified from another machine (`ss` on the host shows nothing — DNAT):
5055→307, 8989→401, 7878→302, 8686→200, 9292→401, 8682→200.

Post-checkpoint database sizes, for the next identity comparison:

| db | bytes |
|---|---|
| /home/edgar/docker-services/sonarr/config/sonarr.db | 960659456 (+69632, WAL folded in) |
| /usr/local/etc/docker-overseer/db/db.sqlite3 | 1044480 (4.1 MB WAL folded in, size unchanged — page updates, not new pages) |

`integrity_check` returned `ok` for all nine databases, run as each container's own UID.
