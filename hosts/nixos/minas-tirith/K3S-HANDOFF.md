# k3s migration — HANDOFF

**Read this first.** Updated 2026-08-07 after the `media` wave landed, so a fresh session
can continue without re-deriving anything. Everything referenced here is committed.

---

## Where things stand

**Phase 0 and Phase 1 are COMPLETE. Phase 3: 15 of 35 services migrated.**

| | |
|---|---|
| on k3s | `audiobookshelf`, `komga`, `palworld`; the **`media` wave (10)**: `tautulli`, `overseerr`, `prowlarr`, `sonarr`, `radarr`, `lidarr`, `animearr`, `maintainerr`, `wrapperr`, `shelfmark`; and **tier A (2)**: `kavita`, `calibre` |
| on docker | **17** containers |
| cluster | 2 nodes Ready, Secret encryption Enabled, CoreDNS 2 replicas |

Health check for a new session:

```sh
ssh pelargir 'sudo k3s kubectl get pods -n media; sudo k3s kubectl get pods -n books'
ssh minas 'sudo docker ps -q | wc -l'               # expect 17
```

Verify ingress against `K3S-BASELINE-MEDIA.md` — **not** against 200. Six of these
hostnames return 303/307/302/401 when perfectly healthy, and `maintainerr.saldivar.io`
returning **200 instead of 401** means the `basic-auth@file` middleware was dropped.

> ⚠️ `ssh minas`, **not** `minas-tirith`. `docker` needs `sudo` there and without it
> reports **zero** containers rather than failing.

---

## THE NEXT TASK

No group cutover is pending. What remains is individually-scoped work:

1. **`readmeabook`** — a database migration, not a manifest translation. One container
   running Postgres 16 + Redis + the app under supervisord, on docker **named volumes**.
   Approach settled in the ledger: cold file copy of stopped volumes, migrate as-is, do
   not decompose. **Its four secrets are empty at source and must STAY empty.**
2. **`media-tracearr-1`** — same shape as readmeabook (embedded Postgres + Redis in one
   container). Do not schedule it as a quick one.
3. **The GPU pair** — `plex`, `jellyfin`. Both are bridged and working; migrate
   individually, each with its own transcode verification.
4. **The privileged VPN pair** — `deluge-vpn`, `deluge-books`. Migrate `deluge-books`
   alone first, proving the kill-switch fails closed before any client is pointed at it.
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

pelargir's checkout is often stale while its **running system is current** — it gets
deployed from elsewhere. Judge the delta by the derivation count in `dry-build`, not by
`git log`.

---

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
  `QBITTORRENT_URL` is already a host IP, which a Pod reaches fine. `readmeabook`, having
  no edges, is free to land in `books`.

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
`.backup` API — sonarr's 960 MB in 2.1 s. A service on k3s that genuinely needed
quiescing could not use the `docker stop` branch and would need a pod-level equivalent
(scale to 0, dump, scale back); not built, because nothing needs it.

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
