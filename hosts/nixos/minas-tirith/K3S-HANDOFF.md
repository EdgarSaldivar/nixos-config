# k3s migration — HANDOFF

**Read this first.** Written 2026-08-07 to let a fresh session continue without
re-deriving anything. Everything referenced here is committed and pushed.

---

## Where things stand

**Phase 0 and Phase 1 are COMPLETE.** Phase 3 is in progress: 3 of 35 services migrated.

| | |
|---|---|
| on k3s | `audiobookshelf` (books), `komga` (media), `palworld` (games) |
| on docker | 29 containers |
| cluster | 2 nodes Ready, Secret encryption **Enabled**, CoreDNS 2 replicas (one per node) |
| repo | pushed to origin, working tree clean |

Health check for a new session:

```sh
ssh pelargir 'sudo k3s kubectl get pods -A'
ssh minas 'sudo docker ps -q | wc -l'          # expect 29
ssh minas "curl -sk -o /dev/null -w '%{http_code}\n' -H 'Host: listen.saldivar.io' https://127.0.0.1/"   # 200
ssh minas "curl -sk -o /dev/null -w '%{http_code}\n' -H 'Host: komga.saldivar.io'  https://127.0.0.1/"   # 200
```

> ⚠️ `ssh minas`, **not** `minas-tirith` — the latter does not resolve.
> `docker` needs `sudo` on minas; without it `docker ps` reports **zero containers**
> rather than failing.

---

## THE NEXT TASK — the `media` wave

Migrate ~10 coupled services in one atomic cutover. **Everything needed is prepared.**

### Why it is atomic (D14 — read this in the plan)

Services reference each other by **bare container name** (`http://prowlarr:9696`,
download-client host `deluge-vpn`). That resolves only via docker's embedded DNS on a
shared network. A k8s namespace restores the same property — **verified**: a Pod in
`media` resolves `http://prowlarr:9696` through the search domain — so configs need **no
changes**, but only once the whole group has moved. A half-migrated group is broken in
both directions.

### The set

**Migrate (10):** `media-tautulli-1`, `overseerr`, `prowlarr`, `sonarr`, `radarr`,
`lidarr`, `animearr`, `maintainerr`, `wrapperr`, `shelfmark`

**Stay on docker, already bridged (4):** `plex`, `flaresolverr`, `deluge-books`,
`deluge-vpn` — deliberately keeping both GPU workloads and both privileged VPN
containers out of the window, to migrate individually later.

**`readmeabook` is EXCLUDED** and needs its own decision — see below.

### ⛔ Service names must match docker ALIASES, not container names

| container | Service name(s) required |
|---|---|
| `media-tautulli-1` | `tautulli` **and** `media-tautulli-1` |
| `overseerr` | `overseer` **and** `overseerr` |

`maintainerr` reaches them as `http://tautulli:8181` and `http://overseer:5055`.
Everything else matches its container name.

### Ground truth

`K3S-PLEXNET-INVENTORY.md` — image digests, mounts, ports, env, healthchecks, traefik
labels and measured working sets for all 12 plex-net services. Captured live.
`readmeabook` and `shelfmark` are not in it; gather those separately.

---

## The cutover procedure (established over 3 migrations)

1. **`k3s crictl pull <digest>` FIRST.** Images live only in *dockerd's* store; containerd
   cannot see them. Skipping this leaves the service down during a multi-hundred-MB pull.
2. Baseline: HTTP status on every hostname + a content anchor (DB size, item count).
3. `docker stop`, then verify `Exited` **and that SQLite `-wal` files are gone** — their
   absence proves a clean checkpoint.
4. `PRAGMA integrity_check` **as the container's UID**, never as root. Root leaves
   root-owned `-wal`/`-shm` the Pod cannot open.
5. Snapshot config **after** stopping, so the archive is consistent.
6. Apply, then confirm the **app's own health endpoint** — not just Pod Ready.
7. Verify ingress with a **fresh connection**, and any `hostPort` **from another machine**.
8. Confirm data identity (byte size or hash) against the baseline before declaring done.

⛔ **For auto-deployed manifests, `nixos-rebuild` IS the apply.** There is no
deploy-now-start-later. Commit at `replicas: 0`, deploy, stop docker, *then* scale up —
otherwise the Pod races the container. (This was nearly learned the hard way on palworld:
two servers briefly ran against one save.)

**Rollback order matters:** neutralise auto-deploy → delete deployments → delete
`k8s-*.yml` routes → *then* `docker start`. The irreversible failure is a k3s restart
resurrecting Pods against docker-owned SQLite.

---

## Manifest invariants (all three migrated services follow these)

- **Digest-pin the image.** `:latest` means migrating and upgrading at once — two
  failure modes that cannot be told apart.
- **`strategy: Recreate`** for anything with hostPath SQLite. RollingUpdate briefly runs
  two Pods on one database and corrupts it on the first routine image bump.
- **`hostPath type: Directory`** — the default type *creates* a missing path, silently
  presenting an empty library.
- **Only compose-declared env.** Restating image defaults freezes them.
- **Size from the resource sampler**, never `docker stats` — that column includes page
  cache and overstates by up to 6×.
- **Preserve published ports via `hostPort`**, and note CNI implements it as PREROUTING
  DNAT, so `ss` on the host shows nothing. Test from another machine.
- **JVM services need `-XX:MaxRAMPercentage`** alongside any memory limit, or the JVM
  sizes its heap from the cgroup limit and OOMs below current usage.
- **No `fsGroup`** — it is a no-op on hostPath (tested).
- **Routes are declarative** in `traefik-routes.nix` (supports multi-host + middlewares).
  Adding a service = one entry, same commit as its manifest.

---

## Open decisions — these need the owner

1. **`readmeabook`.** Embedded Postgres 16 + Redis on docker *named volumes*, not bind
   mounts. Approach is settled (cold file copy, migrate as-is, do not decompose) — see
   the ledger. **Its four secrets are empty at source and must STAY empty in the Pod.**
   Also: `prowlarr` references it by bare name and it publishes no ports, so if it stays
   on docker it needs port 3030 published to be bridgeable.
2. **Namespace for `readmeabook` and `shelfmark`** — books-stack services that the
   reference graph pulls into the `media` wave. Bare-name resolution works only *within*
   a namespace.
3. **Rotate the PIA credentials.** Deliberately deferred. They live in 5 files under
   `/home/edgar/git/docker` (no remote, deleted after migration) and were displayed in
   session. Rotate in the PIA dashboard, update the files, recreate `deluge-vpn`,
   `deluge-books`, `gluetun` **one at a time**, confirming tunnel + non-local egress
   between each.
4. **`prowlarr`'s Deluge client was deleted** (dead `172.20.0.3`). It now has only
   `Deluge-Books`. If prowlarr should also reach `deluge-vpn`, re-add it in the UI.

---

## Traps that cost time — do not re-learn these

**Probe correctness is the recurring failure.** Four wrong conclusions came from bad
tests, not broken systems:

- `ss` shows nothing for a `hostPort` — it is DNAT, not a listener. Check
  `iptables -t nat -L CNI-HOSTPORT-DNAT`, or a `Recv-Q` on the socket inside the pod netns.
- A **Steam A2S probe** gets no reply from palworld because `COMMUNITY=false`.
- A **VPN egress check** through PIA's DNS can time out; test by IP
  (`https://1.1.1.1/cdn-cgi/trace`) to separate DNS from routing.
- **NetworkPolicy tested through traefik appears to have no effect** — traefik pools
  connections and conntrack passes ESTABLISHED flows regardless. Force a fresh TCP
  connection (`docker run --rm busybox nc`) or the result is meaningless.

**Never report a gate as passing when its output was empty.** That happened once here.

**Scan for credentials with a broad pattern** (`USER|PASS|KEY|TOKEN|SECRET|CLAIM|AUTH|
CRED` on the variable *name*). A scan for "password" missed `VPN_PASS` and leaked PIA
credentials into a commit — caught by cross-review, purged before push.

**`git add` before `nix build`** — flakes only see tracked files.

**Codex workers check out HEAD**, so an uncommitted file is invisible to review. Commit
first or the review is of your description, not your code.

---

## What is already built and working

- **4 docker bridges** in `manifests/docker-bridges.yaml` — selectorless Service +
  manual `discovery.k8s.io/v1` EndpointSlice (NOT `v1 Endpoints`, deprecated in 1.33).
  All verified 200 from a Pod. ⚠️ **Not health-aware** — they keep advertising
  `10.0.1.6` after the docker process stops.
- **`coredns-custom`** maps minas' 27 public hostnames to `10.0.1.6` so Pods reach
  still-on-docker services without hairpinning. Named hosts only —
  `ha-pelargir.saldivar.io` must keep resolving to pelargir. **CoreDNS must be restarted**
  for changes to take effect; `reload` watches the Corefile, not imported files.
- **`profiles: ["migrated"]`** on each migrated service in its compose file, so
  `docker compose up -d` cannot resurrect it against a Pod's hostPath. Rollback is
  `docker compose --profile migrated up -d <svc>`.
- **GPU**: RuntimeClass `nvidia`, `nvidia.com/gpu` schedulable with 2 time-sliced
  replicas, real transcode verified in a Pod. NVENC saturates ~4 concurrent 1080p; the
  driver session cap is 12.
- **Secrets**: `secrets/cluster-apps.yaml` (sops; admin + pelargir only) renders to
  tmpfs and is applied from there. Pattern is established — palworld uses it.

---

## Reference

| file | what it holds |
|---|---|
| `K3S-MIGRATION-PLAN.md` | phases and decisions **D1–D15** — D14/D15 are the important ones |
| `K3S-MIGRATION-LEDGER.md` | all 35 services, measured resources, per-service hazards |
| `K3S-PLEXNET-INVENTORY.md` | live ground truth for the 12 plex-net services |
| `K3S-PHASE1-PLAN.md` | Phase 1, all 5 gates with evidence |
| `K3S-PC1-BASELINE.md` | pre-change cluster baseline; deviation = abort |
| `pelargir/ROLLBACK.md` | ⛔ opens with the encryption check — read before any rollback |
| `pelargir/restore-drill-vm.nix` | the control-plane restore drill, repeatable |
