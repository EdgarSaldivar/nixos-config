# minas-tirith: Docker Compose → k3s migration plan (v4)

**Status:** v4, 2026-08-06.

> **⚠️ REVIEW STATUS — read before trusting this document.** Codex reviewed **v1** (round 1)
> and **v3** (round 2, verdict: *"still not executable"*). **v4 has NOT been reviewed.**
> Its central change — abandoning the three-server etcd design for single-server-recoverable
> — came from round 2's finding plus owner decisions, and has not been re-checked by a
> second model. Treat D8 and P0.1/P0.2 as decided-but-unreviewed.

v2 was produced after an adversarial cross-model review
(gpt-5.6-sol, high effort, 848k tokens, 541s; verdict on v1: *"sound but needs specific
changes first"* — preserved as `K3S-MIGRATION-REVIEW.md`). v3 corrects three items in v2
that were verified against the live cluster rather than inferred.

**Owner decisions**
- Migrate all **currently running, non-parked** services in the migration ledger to k3s
  (not the oci-containers alternative). Parked and excluded workloads are listed in §0b
  and §1 — "ALL" previously contradicted both.
- ~~`osgiliath` is the third server node~~ — **REVERSED in v4.** There is no third
  server; see D8. Osgiliath is decoupled and non-blocking.
- **Control plane stays a single server, made recoverable** (D8 v4).
- **PinCollector parked**, P0.11 dropped (see §0b).
- **Jellyfin backups must have zero degradation** — accepted the brief stop, rejected a
  permanently-degraded dump.

---

## v2 → v3: corrections from checking the live cluster

All three were cases of asserting a problem that measurement did not support. Recorded
because two of them made this plan look harder than it is.

| v2 said | Measured reality | Effect |
|---|---|---|
| D8: osgiliath's `wifi.nix` makes it a poor etcd member | `wifi.nix:1` documents it as *"a lower-priority recovery path"*; primary is MAC-matched `10-ethernet`, wifi is `20-wifi-later` at a higher metric | D8 **softened** — the only requirement is that osgiliath is physically wired. Keep `wifi.nix`. |
| D3: the cert/reflection stack needs redesigning, and the CF token may lack `Zone:Zone:Read` | The `letsencrypt` ClusterIssuer is **Ready** and its solver selector is `dnsZones: [saldivar.io]` — the **whole zone**. Two certs are already issued via DNS-01 (`pelargir-wildcard`, `osgiliath-home-assistant`), so the token demonstrably works. Reflector is deployed. | D3 **downgraded** from "redesign" to "add a Certificate object + widen reflector's namespace list". No new issuer or token. |
| Jellyfin's dump is permanently degraded | Fixed and verified on a real run: `sqlite backup: …library.db (8 tables)` + `restarted jellyfin after consistent dump`, run `Result=success`, **no degraded marker** | Closed. See P0.6. |

---

## v1 → v2: what changed and why

The review found four factual errors and several structural gaps. All corrected here.

| v1 claim | Reality | Evidence |
|---|---|---|
| A `*.saldivar.io` cert exists and reflector propagates it | Cert covers **`ha-pelargir.saldivar.io`, `pelargir.saldivar.io`, `*.pelargir.saldivar.io`** only; reflector is scoped to the **`home`** namespace alone | `hosts/nixos/pelargir/manifests/ingress.yaml:247` and `:241`, both re-verified directly |
| "101 ingress routes" are the bulk of the work | 101 counts **label keys**; a router carries several. ~30 real hostnames. The bulk is dependency-aware per-service migration | review, §Phase ordering |
| Add minas as a 2nd server for HA | A 2-member etcd needs **both** for quorum → zero fault tolerance. Needs **three** | review Q1 |
| Acceptance: "no plaintext credentials on disk" | False — Secret values sit unencrypted in the k3s datastore unless encryption-at-rest is enabled | review Q4 |

New sections addressing gaps v1 missed entirely: service discovery (**D9**),
transactional rollback (**D10**), scheduling isolation (**D7**), three-server control
plane (**D8**), and Kubernetes-aware backup/monitoring (**P0.6**).

---

## 0. Ground truth (measured, not assumed)

| | |
|---|---|
| Services declared across 6 compose projects | 67 |
| Services actually running | **35** |
| Data-pinned to minas (`/storage*` bind mounts) | **16** |
| Bind-mounts into `/storage*` / `/etc`+`/usr/local` | 31 / 31 |
| `privileged: true` | 2 (`deluge-vpn`, `deluge-books`) |
| `cap_add` / `NET_ADMIN` | 9 |
| `network_mode:` | 7 (incl. `network_mode: service:gluetun`) |
| `devices:` blocks (GPU/dri) | 5 |
| `depends_on` / `healthcheck` | 5 / 11 |
| traefik router **label keys** (≈30 distinct hostnames) | 101 |

**Cluster today**
- `pelargir` — Pi 5, **single server, sqlite datastore**, k3s v1.35.6+k3s1.
  **TAINTED `node-role.kubernetes.io/control-plane:NoSchedule`** as of 2026-08-06 (P0.7);
  its four `home` workloads carry matching tolerations and are 1/1. Also carries
  `--node-label svccontroller.k3s.cattle.io/enablelb=true` (`hosts/nixos/pelargir/k3s.nix`)
- `minas-tirith` — agent. 32 threads, 125 GB RAM, RTX 2080 (driver 595.71.05),
  containerd 2.2.5-k3s2
- `osgiliath` — **configured but NOT deployed.** In the flake as **`x86_64-linux`**;
  full config exists (`disko.nix`, `k3s.nix`, `INSTALL-RUNBOOK.md`); currently
  `role = "agent"` with taint `osgiliath.saldivar.io/workloads=true:NoSchedule`
  (`hosts/nixos/osgiliath/k3s.nix:22`). Not on the tailnet, not in the cluster.
- k8s workloads on minas: **0** — `svclb-traefik` moved to pelargir once the ServiceLB
  allow-list took effect (P0.8), so nothing contends for `:80`/`:443` on minas.
  Docker on minas: **32** containers (was 35: readarr dropped, host-hostnames removed as
  incompatible, PinCollector's three parked). All real work still happens there.
- Flux: not deployed.
- **cert-manager stack is healthy and broader than v1/v2 assumed** (verified 2026-08-06):
  ClusterIssuer `letsencrypt` Ready with `dnsZones: [saldivar.io]`; certs
  `pelargir-wildcard` (expires 2026-11-03) and `osgiliath-home-assistant` both Ready;
  reflector running in `kube-system`. Existing namespaces: `cert-manager`, `default`,
  `home`, `kube-node-lease`, `kube-public`, `kube-system`, `osgiliath`.

---

## 0b. Backlog — parked, not forgotten

**PinCollector (`~/PinCollector/infra`) — STOPPED 2026-08-06, owner decision.**
`infra-api-1`, `infra-minio-1` and `infra-postgres-1` were stopped and are parked
pending a separate decision about where PinCollector lives. `model-service` was
already not running.

- Stopped **by container name, not `docker compose stop`** — `~/PinCollector/infra`
  and `~/git/docker/infra` share the compose project name `infra`, so a compose
  stop in one can take the other's containers (traefik2) with it. Verified
  traefik2 stayed up.
- `restart=unless-stopped`, so they stay down across reboots without further action.
- Data intact and untouched on ZFS: `storage2/pincollector/postgres` (18 MB),
  `storage2/pincollector/minio`, `storage/pincollector/minio`.
- Removed from the heartbeat's critical-services list and the running-count
  threshold lowered 35 → 28, because alerting on a state you chose is how an
  alert becomes noise. **Re-add both when the stack returns.**
- Brings P0.9 (registry for the two `:local` images) with it — see P0.9.

Also parked: the **Nextcloud 28 → 34 upgrade ladder** (six majors, its own
project), and the **`wolf` gameserver**, which uses `runtime: nvidia` and would
need the same CDI conversion as plex/jellyfin if ever started.

---

## 1. Non-goals

1. Do not migrate the 14 non-running gameservers (15 declared, only `palworld` ran).
2. Do not do the Nextcloud 28→34 ladder here. Migrate **at 28**; upgrade separately.
3. No Flux in phases 0–5. Plain manifests first (owner preference); revisit in Phase 6.
4. Do not delete `/storage2/backup-2026-07-30` (298 GB) until every service is verified.

---

## 2. Decisions

### D1 — Storage: `hostPath` + hostname `nodeSelector` · CONFIRMED
Data physically exists only on minas; no abstraction changes that.
**Additions required by review:** `hostPath.type: Directory`, never
`DirectoryOrCreate` — an auto-created empty mountpoint is a *demonstrated* destructive
failure mode on this host (`containers.nix:62` already gates Docker on ZFS readiness;
k3s has no equivalent). Select on **hostname**, not a convenience `storage=zfs` label
(v1 was inconsistent). `replicas: 1` + `strategy: Recreate` for every single-writer
workload. **No `fsGroup`** that would recursively chown the media tree. New database
PVCs on `local-path` must also be pinned to minas, capacity-checked, backed up, and
given deletion protection — `local-path` is not automatically safe.
**Deliverable and owner (was missing):** the only StorageClass today is `local-path`
with `reclaimPolicy: Delete`, so deleting a PVC destroys the data. `K3S-PHASE1-PLAN.md`
**P1D** owns this — static local PVs with `Retain`, node affinity and a tested
rebind/restore — and no stateful workload migrates before it lands.

### D2 — GPU: **NVIDIA device plugin**, not CDI · REVERSED
v1 chose CDI because it is proven under Docker. That proves the host toolkit, not the
Kubernetes device-request path — and (at the time) three consumers (plex, jellyfin,
model-service). **Measured 2026-08-06: only TWO are live** — plex and jellyfin; the GPU
sits idle at 1 MiB/8192 MiB, 0%. `model-service` is parked with PinCollector, and
`immich` runs a `-cuda` image while requesting no GPU at all. The device plugin is still
right, but size time-slicing for two consumers, not three
sharing one RTX 2080 via `nvidia.com/gpu=all` leaves the scheduler blind. Deploy the
plugin, request the extended resource, and choose **explicitly** between exclusive
allocation and configured time-slicing, documenting that time-slicing gives scheduling
accounting but **not** memory or fault isolation.

### D3 — Ingress/TLS: add a Certificate, widen reflector · DOWNGRADED in v3
v1 assumed a `*.saldivar.io` cert that does not exist. v2 over-corrected into "redesign
the cert stack". Measurement says the foundation is **already sound**:

- ClusterIssuer `letsencrypt` is **Ready**, and its solver selector is
  `dnsZones: [saldivar.io]` — the **entire zone**, not just pelargir names
  (`hosts/nixos/pelargir/manifests/ingress.yaml:219-227`).
- Two certificates are already issued through it via DNS-01 (`pelargir-wildcard`,
  `osgiliath-home-assistant`), which **proves the Cloudflare token has sufficient
  scope** — the long-standing `Zone:Zone:Read` worry is settled.
- Reflector is deployed and running in `kube-system`.

What is actually narrow is only (a) the existing **Certificate object**, scoped to
`ha-pelargir.saldivar.io`, `pelargir.saldivar.io`, `*.pelargir.saldivar.io`
(`ingress.yaml:247`), and (b) reflector's `reflection-allowed-namespaces` /
`reflection-auto-namespaces`, both set to `home` only (`ingress.yaml:241`).

**Deliverable (Phase 1):** one additional Certificate covering the ~30 media hostnames,
plus reflector's namespace list extended to every namespace that terminates TLS.
**Constraint:** a `*.saldivar.io` SAN does **not** cover `admin.pin.saldivar.io` — two
labels deep — so that needs its own SAN or a `*.pin.saldivar.io` alongside.
**Do not issue before the namespace layout is fixed** (P0.5/P0.10): issuing production
certs for hostnames nothing serves burns Let's Encrypt rate limit. Use LE **staging**
for first issuance, per `INSTALL-RUNBOOK.md`.

Route conversion is unchanged and still real work: inventory **routers, middleware
chains, priorities, entrypoints, TLS options, backend scheme/port, websockets,
redirects, auth and allowlists** — not label counts. Explicitly cover non-HTTP exposure
(game server UDP/TCP, VPN/downloader ports, media discovery); HTTP IngressRoutes cannot
replace all compose port publishing.

### D4 — Secrets: sops-nix renders Secret manifests · CONFIRMED, spec expanded
Valid because Nix activation is a legitimate declarative deploy mechanism, not merely
because Flux is absent — `hosts/nixos/pelargir/secrets.nix:51` already does this.
**What v1 omitted: who decrypts and who applies.** `secrets/minas-tirith.yaml` is
decryptable by minas, but minas is an agent with no deploy credential (`.sops.yaml:47`).
Either render application secrets on a server recipient or issue a tightly scoped deploy
identity. Apply directly from `/run`; do **not** copy plaintext manifests to persistent
storage. Handle rotation, pruning, and escaping of arbitrary `.env` values — prefer
generated JSON/base64 over interpolating secrets into quoted YAML. **Enable Kubernetes
secret encryption at rest.** Delete the plaintext `.env` files and add a `*.env` rule to
that repo's `.gitignore` (absent today — `git add -A` would commit credentials).

### D5 — Cutover unit is the **dependency group**, not the service · REVISED
Stateless leaves may move alone; tightly coupled components and application/database
pairs move together. **At no point may both copies write the same state.**

### D6 — Databases: dump/restore · CONFIRMED, tightened
Use a **final dump taken after stopping writers**, not the preceding nightly dump.
Restore into the same PostgreSQL/extension family (Immich needs pgvector —
`RESTORE-RUNBOOK.md:277`), fail on SQL errors, validate row counts, and coordinate
application data with the database.

### D7 — Scheduling isolation · NEW
Pelargir is schedulable and untainted, and with no requests everything is BestEffort, so
the scheduler cannot see the 125 GB/32-thread vs Pi disparity. Before the pilot: **taint
pelargir** against migrated application workloads (osgiliath's `k3s.nix:22` shows the
taint-by-default pattern to copy); **audit every image for amd64/arm64** — pelargir is
ARM while minas and osgiliath are `x86_64-linux`, so amd64-only images must never land
on the Pi; add realistic CPU/memory/ephemeral-storage requests derived from Docker
observations; reserve headroom for k3s, Traefik, CoreDNS, storage and backup.

### D8 — Control plane: **single server, made recoverable** · SUPERSEDED v3→v4
> **⚠️ v4 CORRECTION (2026-08-06). The three-server embedded-etcd design below is
> ABANDONED. Do not build it.** It survived into v3 only because the plan was not
> revised after the decision. Kept visible rather than deleted so the reasoning is not
> rediscovered.

**Why it was abandoned, in order of decisiveness:**
1. **k3s does not support embedded etcd across distributed sites.** All three hosts are
   at *different* sites; the docs require servers be co-located and mutually reachable on
   private IPs. Tailscale satisfies the letter (private IPs, direct paths) but this is an
   explicitly unsupported deployment shape.
2. **The repo cannot express it.** `modules/nixos/fleet/k3s-node.nix` hard-asserts
   `serverAddr == ""` when `role = "server"`, so a *joining* server is unrepresentable;
   only 6443/10250 are opened (etcd peers need 2379–2380); and minas and osgiliath hold
   the **agent** token, which cannot join servers.
3. **No third viable member exists.** `nardol` is the only other always-reachable x86 box
   and it is *intermittently powered* (primarily a game-streaming host) — an etcd member
   that is off half the time is worse than absent. `osgiliath` is on **broadband**, whose
   jitter and brief outages are exactly what triggers spurious Raft elections; in a
   3-member cluster the least reliable member drives instability.
4. **HA buys little here.** 16 services are pinned to minas by data locality, Frigate to
   osgiliath, Home Assistant to pelargir. An HA control plane keeps none of them alive
   when their node dies — it only preserves the ability to *manage* during an outage.

**What replaces it:** keep pelargir as the single server and make it *recoverable*.
DONE 2026-08-06 — the datastore (22 MB) plus the server token are now captured by
SQLite's online `.backup` into every restic snapshot and sent offsite to minas. Before
that, nothing copied them: a Pi failure lost the cluster outright. A pelargir failure is
now "restore 22 MB onto new hardware", which is most of what HA would have provided.

**Revisit HA only if TWO** stable, always-on, wired hosts appear at one site alongside
pelargir — three co-located servers is the supported shape, and remote nodes stay agents,
which is what minas already is and what works today.

**Measured, for whoever revisits this:** pelargir↔minas over the tailnet is
min 14.3 / avg 16.4 / max 18.8 ms, jitter 1.2 ms, 0% loss, **direct** (not DERP-relayed).
Latency was never the blocker — support status and member reliability were.

<details><summary>Superseded three-server rationale (v2/v3) — do not implement</summary>

**v3 correction:** v2 flagged osgiliath's `wifi.nix` as disqualifying. It is not.
`wifi.nix:1` documents it as *"a lower-priority recovery path"* — the primary is
MAC-matched `10-ethernet`, and wifi is `20-wifi-later` carrying a higher DHCP metric, so
it is a fallback that only takes over if the wire is gone. Keep the file. The residual
requirement is operational, not architectural: **osgiliath must be physically wired**,
because an etcd member that fails over to wifi and flaps costs quorum. Verify the link
before promoting it to server, and treat "osgiliath running on wifi" as an alert
condition rather than a supported steady state.

Also note the arch split this creates: pelargir is ARM, minas and osgiliath are
`x86_64-linux`. That is an advantage — amd64-only images have somewhere to go besides
minas — but it makes D7's architecture audit mandatory rather than optional.

</details>

### D9 — Service discovery must be designed before anything moves · NEW
Containers today resolve each other by name on shared docker bridges (`traefik-net`,
`plex-net`, `books-net`, `nextcloud-net`, `s3-net`, `infra_default`). Kubernetes does not
preserve this, and **during coexistence Docker cannot resolve k8s Service names and k8s
pods cannot resolve docker aliases.**
**Deliverable:** a before/after matrix — caller, current hostname/network/port,
transition endpoint, final Service DNS name, protocol, auth, health semantics — for
every cross-service edge. Notably the `*arr` suite ↔ download clients ↔ indexers, and
every app ↔ database/Redis/MinIO edge. Cross-namespace callers need
`service.namespace.svc`. Internal traffic must **not** be routed through public ingress
as an accidental substitute for discovery.
*Without this, the pilot passes TLS while the applications silently stop talking.*

### D10 — Transactional rollback per stateful cutover · NEW
"Stop the docker copy" is not a rollback plan. Each stateful cutover defines:
1. quiesce all writers; 2. record a final dump/snapshot identifier **and image digest**;
3. start exactly one k8s writer; 4. state whether rollback preserves k8s-era writes
(reverse dump/sync) or discards to the recorded point; 5. **prove the old image can read
the post-cutover schema** before allowing reverse start. Applications that migrate SQLite
schemas can render the old image unusable even against the same hostPath.

### D11 — Compose semantics that do not survive translation · NEW
`depends_on` (5) and healthchecks (11) have no Kubernetes equivalent that waits.
Applications must tolerate retry, and probes must be classified correctly as
startup/readiness/liveness — copying a compose healthcheck into an aggressive liveness
probe creates destructive restart loops. Also inventory UIDs/GIDs (services here run as
911 and 1000 — `RESTORE-RUNBOOK.md:172`), supplemental groups, `/dev/shm`, timezone
mounts, sysctls, ulimits, stop signals and grace periods.

---

### D12 — Per-service cutover runs through docker traefik2, via cluster DNS · NEW, PROVEN 2026-08-06

The plan defers moving `:80`/`:443` to k3s traefik until Phase 6. That left an unanswered
question underneath Phases 2-5: **once a service is a Pod, how does public traffic reach
it?** k3s traefik cannot serve it — measured, its LoadBalancer address is
`100.78.255.101`, pelargir's *tailscale* IP, with `externalTrafficPolicy: Local` and its
only pod and svclb on pelargir. It is not reachable from the LAN or the internet at all.
Public ingress is still entirely the **docker `traefik2`** container on minas.

So the cutover path has to be traefik2 -> Pod. That was untested, and it is the mechanism
the whole incremental migration depends on. Measured, on this cluster:

| path | result |
|---|---|
| minas host -> Pod IP | ✅ |
| minas host -> ClusterIP | ✅ |
| docker `bridge` -> ClusterIP | ✅ |
| docker `traefik-net` (traefik2's own network) -> ClusterIP | ✅ |
| docker `traefik-net` -> Pod IP | ✅ |

**And cluster DNS works from docker, additively.** Docker's embedded resolver cannot
resolve `*.svc.cluster.local` (it times out), but adding `dns: [10.43.0.10]` fixes that
*without* breaking container-name resolution — verified side by side, docker names
resolve identically with and without it, because the embedded resolver at 127.0.0.11
still answers names it knows and forwards only the rest upstream.

**Therefore each service migrates like this, with no ingress rework and no touching
`:80`/`:443`:**

1. Add `dns: [10.43.0.10]` to traefik2 (once, for all services).
2. Bring the service up as a Pod + ClusterIP Service, alongside the running container.
3. Repoint that one traefik2 backend from `http://jellyfin:8096` to
   `http://jellyfin.media.svc.cluster.local:8096`.
4. Verify, then stop the docker container -- do not delete it (Phase 2 exit criterion).

Rollback is repointing one backend back to the container name, which is why this is worth
having established before anything moves.

⚠️ **Use cluster DNS names, not ClusterIPs.** A ClusterIP is stable for the life of a
Service object but is reassigned if the Service is ever deleted and recreated, which is
routine while iterating. A hardcoded IP then silently routes to nothing, or worse, to
whatever later claims the address.

### ✅ INGRESS CANARY PASSED — 2026-08-06

External HTTPS request → docker `traefik2` → **k3s Pod**, HTTP 200, confirmed in the
access log as `"k8s-canary@file" "http://10.43.161.157:80"`. The Phase 2 ingress gate is
met and the D12 path is proven end to end, not just at the socket level.

Four things the canary established that the plan had wrong or unstated:

1. **The file provider DOES watch.** Routes were added *and* removed live, with no
   traefik restart and no interruption to the other 31 services. So per-service cutover
   costs zero downtime on the ingress side — the concern that traefik2 would need
   recreating (taking all public ingress down briefly) does not apply.

2. ⛔ **The entrypoints are `http` and `https` — NOT `web`/`websecure`.** Using the
   documentation-default names fails with `EntryPoint doesn't exist`, and the router is
   silently dropped while the request still returns a plausible-looking 301 from the
   entrypoint's global redirect. Every Phase 3 route file must use `https`.

3. ⛔ **`traefik.yml` in the dynamic directory contains STATIC config that traefik
   ignores.** Its `entryPoints`, `certificatesResolvers` and `accessLog` sections are
   never read — the real values come from the CLI flags. That file is misleading:
   it *looks* like it defines `web`/`websecure`, which is exactly why (2) is a trap.
   Only its `http.middlewares` section is live.

4. **The `https` entrypoint already carries a `*.saldivar.io` wildcard certificate**
   (`--entrypoints.https.http.tls.domains[0].sans=*.saldivar.io`, resolver
   `dns-cloudflare`). **This defers D3's Phase 1 certificate work.** A migrated service
   keeps its TLS with no cert-manager change, because ingress stays on traefik2 until
   Phase 6 — so the "~30 media hostnames" Certificate is a **Phase 6** deliverable, not
   a Phase 1 blocker. (Still true that `*.saldivar.io` will not cover
   `admin.pin.saldivar.io`, two labels deep, when that day comes.)

### Follow-ups completed 2026-08-06 (after the audiobookshelf pilot)

**traefik2 now has cluster DNS.** `dns: [10.43.0.10, 1.1.1.1]` added to its compose;
recreate took **1 second** and all 27 routes came back byte-identical. Verified additive
in all three directions afterwards — cluster names resolve
(`audiobookshelf.books.svc.cluster.local` → 10.43.77.182), container names still resolve
(`jellyfin` → 172.16.1.18), public names still resolve. `1.1.1.1` is a deliberate
fallback: listing only CoreDNS would make every lookup traefik performs — including the
Let's Encrypt DNS-01 challenge — depend on the k3s cluster being up.

**So Phase 3 routes use cluster DNS names, never ClusterIPs.** The audiobookshelf route
was switched to `http://audiobookshelf.books.svc.cluster.local:80`. A ClusterIP is
reassigned if a Service is deleted and recreated, after which a hardcoded IP routes
silently into a black hole.

### ⛔ SECURITY: the traefik dashboard was served unauthenticated

Found while diffing routes after the recreate — `traefik.saldivar.io` changed 302 → 401,
which was *not* caused by the change being tested. Two routers matched that host:

| router | middleware | auth |
|---|---|---|
| `traefik@docker` (traefik2's own labels) | `strip-traefik` (stripPrefix only) | **none** |
| `traefik-router@file` | `basic-auth` | yes |

Identical Host rules mean identical priority, so which one won was arbitrary and flipped
on restart. Whenever the docker one won, the dashboard — every router, service and
backend address in the fleet — was reachable **without credentials**. The duplicate
labels are removed; the file router with `basic-auth` is now the only one, and the
dashboard returns 401 consistently.

This is worth generalising: **duplicate routers across providers fail silently and
non-deterministically.** Phase 3 adds a file-provider route per migrated service while
docker-label routes still exist, so every cutover must confirm the old label route is
actually gone rather than merely shadowed.

**jupyter removed entirely** (no longer needed): it had no container, image, compose
entry or data left — only a dead route pointing at `192.168.6.59:8888`. Router and
service deleted. `dungeon.saldivar.io` is in the same orphaned state (dead backends at
`192.168.6.94`) and was left alone pending a decision.

✅ Resolved: `traefik.yml.bak` has been moved out of the watched directory to
`infra/traefik-backups/`. A `.bak` beside live config is exactly the duplicate-router
accident described above.

⚠️ **This makes traefik2 depend on CoreDNS.** That dependency is the reason the CoreDNS
single-replica SPOF (A.7) had to be fixed first: with one replica on pelargir, a pelargir
reboot would have broken name resolution for the *docker* ingress path too, taking down
services that had not even migrated yet.

### D13 — The REVERSE path: a migrated Pod reaching services still on docker · NEW, MEASURED 2026-08-06

D12 proved traffic *into* the cluster. This is the other direction, and it constrains
migration **order**: during Phases 2-5 most dependencies are still docker containers.

| path | result |
|---|---|
| Pod → docker container IP (`172.16.1.18:8096`) | ⛔ **BLOCKED** |
| Pod → minas host IP + **published** port (`10.0.1.6:8096`) | ✅ |
| Pod → traefik2 on `10.0.1.6:443` | ✅ |
| Pod resolves public names (`jellyfin.saldivar.io`) | ✅ → `99.64.240.101` (the **public** IP) |

**Pods cannot address docker containers directly.** Docker's FORWARD policy drops
traffic that did not originate on its own bridges, so container IPs and container
*names* are both unusable from a Pod. This is expected, not a misconfiguration, and it
is not worth "fixing" by loosening docker's firewall.

**So a migrated service reaches a docker dependency by one of:**

1. **Host IP + published port** — simplest, and correct where the port is published.
   ⚠️ Check the ledger's Ports column first: many services publish **nothing**
   (`media-tautulli-1`, `maintainerr`, `overseerr`, most of the `*arr` mesh) and are
   reachable only through traefik2. For those this option does not exist.
2. **Through traefik2** on `10.0.1.6:443` with the service's normal hostname — works for
   anything that already has an ingress route, which is most of them.
3. **Move the dependency group together** (D5), avoiding the crossing entirely.

⚠️ **Option 2 needs a DNS override, or it hairpins.** A Pod resolves
`jellyfin.saldivar.io` to the *public* address, so the request leaves the network and
must come back through NAT loopback — fragile and slow. The fix is a `coredns-custom`
ConfigMap mapping the affected hostnames to `10.0.1.6`, which is a Corefile fragment and
therefore the one CoreDNS customisation that IS upgrade-safe and k3s-supported. Both
replicas already mount that ConfigMap (`optional: true`).

⛔ Map **named hosts only**, never the whole zone: `ha-pelargir.saldivar.io` lives on
pelargir, so a blanket `*.saldivar.io → 10.0.1.6` would break Home Assistant.

### ✅ RESOLVED 2026-08-07 — `coredns-custom` ships the mapping

| name | before | after |
|---|---|---|
| `kubernetes.default` | 10.43.0.1 | 10.43.0.1 (unchanged) |
| `github.com` | public | public (unchanged) |
| `jellyfin` / `tautulli` / `komga`.saldivar.io | **99.64.240.101** (public — hairpin) | **10.0.1.6** |
| `ha-pelargir.saldivar.io` | Cloudflare | Cloudflare (**falls through**) |

Functionally verified, which is the part that matters: a Pod now reaches
`tautulli.saldivar.io` (**303**), `plex` (**401**) and `komga` (**200**) — application
responses, not network failures. tautulli publishes **no host ports at all**, so before
this a Pod could not reach it by any route.

**Implementation notes worth keeping:**

- The hostname list lives in `minas-tirith/traefik-hostnames.nix` and is imported by
  BOTH `networking.hosts` and the generated ConfigMap, so the host and the cluster
  cannot drift about where a service lives.
- Delivered as a **`.server`** file, not `.override`. k3s' Corefile has
  `import …/custom/*.override` *inside* the `.:53` block and `…/*.server` at top level.
  The main block already uses the `hosts` plugin for NodeHosts, and a second `hosts` in
  one block is a config error — hence a dedicated server block for the zone, with
  `fallthrough` so unlisted names resolve normally.
- **CoreDNS must be restarted** for the change to take effect. The `reload` plugin
  watches the Corefile, not imported files, so applying the ConfigMap alone changes
  nothing and looks like a broken config.
- Entries stay correct after a service migrates: traefik2 remains the ingress until
  Phase 6, so the name still points at 10.0.1.6 and traefik2 forwards to the Pod.

## 3. Phases

### Phase 0 — Prerequisites (**COMPLETE 2026-08-06**)
> Not all entries are blocking, and saying so was wrong: P0.1 is decoupled, P0.2 and
> P0.11 are dropped, P0.9 is deferred. The genuinely blocking set — P0.3–P0.8, P0.10 —
> is done.
- **P0.1 Deploy osgiliath** — **NO LONGER BLOCKING.** It was P0.1 solely to be the third etcd member; D8's v4 correction removes that need. Decoupled into its own track: do when convenient, ideally before Phase 3 so amd64-only images have a second home, explicitly NOT a gate on the minas migration. Its 4 pods stay Pending until then, which is inert.
- ~~**P0.2 Convert to 3-server embedded etcd**~~ — **DROPPED (see D8 v4 correction).** The topology is unsupported by k3s across sites, unrepresentable by the fleet module, and has no viable third member. Replaced by making the single server recoverable, which is DONE.
- **P0.3 Datastore backup + PROVEN restore drill** — ✅ **COMPLETE 2026-08-06.** Note this is SQLite, not etcd; earlier text said "etcd snapshots" and was wrong.
  - Backup: SQLite online `.backup` of `state.db`, plus the server token **and** `/etc/rancher/node/password`, into every restic snapshot, offsite to minas.
  - **Drill passed with identity proof, not counts.** Restored from the OFFSITE repo on minas using credentials derived on the Mac from the admin age key — pelargir treated as lost — and booted in an isolated VM (`restrict=on`, no forwards, no host shares, secrets kept off the nix store).
  - **CA fingerprint `6C:02:09:C4:0C:A2:CB:3F:49:34:71` matched live exactly**, and the `cert-manager/cloudflare-api-token` data hash `6d8691e8c102e38e` matched. A fresh k3s generates a NEW CA, so this proves the restored cluster identity rather than a new database with coincidentally matching object counts. Objects: 7 namespaces / 22 secrets / 18 configmaps / 16 deployments, and the four real `home` workloads by name.
  - Four defects found and fixed by running it: `/etc/rancher/node/password` was **not backed up** (control plane starts, node never rejoins); `--disable-agent` does not exist in v1.35.6 (so a scratch server on minas would have collided with the live agent); SQLite cannot read a WAL db from a read-only mount; k3s refuses to start with no default route.
  - Still unproven, deliberately: full NODE recovery (registration/readiness on replacement hardware) and application-data restore. This drill covers the CONTROL PLANE.
- **P0.4 Fix pelargir's offsite backup** (MINAS-PREP §2/§3: the restic timers fire at a destination that does not exist and skip *quietly*).
- **P0.5 Service dependency matrix** (D9). Highest-value artifact in this plan.
- **P0.6 Replace Docker-specific backup and monitoring.** `system.nix:235` backs up `/var/lib/docker/volumes`, discovers postgres only via Docker, and records `docker-unavailable` once Docker stops (`system.nix:289`); `monitoring.nix:171` counts Docker containers. Both go blind at cutover.
  **The k8s replacement must preserve a behaviour added 2026-08-06:** the sqlite dump
  now *stops the owning container for the duration of its own dump* and restarts it
  immediately, with a trap so a crash cannot leave the service down and a `CRITICAL`
  marker (not the soft `degraded` one) if a restart fails. This exists because jellyfin
  holds `library.db` write-locked for its entire runtime — a busy timeout was tried and
  still failed after waiting the full 61s. Owner requirement is **zero degradation**, so
  the k8s version needs the same quiesce-then-dump semantics (a pod-level equivalent:
  scale to 0, dump, scale back), not a naive file copy of a live SQLite database.
- **P0.7 Scheduling isolation** (D7): taint pelargir, arch audit, resource requests.
- **P0.8 Resolve the `:80`/`:443` overlap and design coexistence routing** — how an unchanged hostname reaches Docker traefik for one service and k3s traefik for another when both cannot own the same address:port. v1 was internally inconsistent (Phase 2 assumed k3s ingress while Phase 5 postponed the port move).
- **P0.9 Image path for the local builds** — ~~blocking~~ **DEFERRED 2026-08-06 with PinCollector itself (see Backlog).** Recorded because the constraint is permanent, not because it gates anything now: `pin-collector-api:local` (682 MB) and `pin-collector-model-service:local` are built from source by `~/PinCollector/infra/docker-compose.yml` and exist ONLY in docker's image store. k3s runs its own containerd with a separate store — measured: 47 images visible to docker, **0** pin-collector images visible to k3s. Any migration of those two needs a registry, a `ctr images import`, or an in-cluster build first.
- **P0.10 35-row migration ledger.** v1's inventory contradicted itself: `qbittorrent-books` pinned but in no phase; `shelfmark`/`readmeabook` pinned then classed stateless; `komga`/`prowlarr` in the hostPath tier but absent from the pinned list. Reconcile against the live host.
- ~~**P0.11 Commit the 2026-08-06 compose fixes**~~ — **DROPPED 2026-08-06 (owner decision).** The target is NixOS/k8s manifests in this repo, so `~/git/docker` is transitional and will not be versioned. Verified before dropping that the rollback baseline is still safe: `~/git/docker` sits inside the nightly backup at `/storage2/backup/minas-tirith/home/edgar/git/docker`, so today's fixes (readarr removal, CDI GPU, deluge nft entrypoints, the `~`→absolute path correction, komga TZ, nextcloud pin) survive loss of the root disk. They are backed up, just not version-controlled.
  ⚠️ Consequence to accept knowingly: there is **no diff history** for that tree — its only commit is from 2023-03-23 and every stack directory is untracked. Recovery means restoring files, not reading a log.
- **P0.11a Fresh backup + ZFS snapshot as the rollback point** — retained, since the rollback target still needs to be a known-good captured state.

### Phase 1 — Foundations
Superseded in detail by **`K3S-PHASE1-PLAN.md`**. Namespaces; NVIDIA device plugin (D2);
secret pipeline + encryption at rest (D4); **add a Certificate + widen reflector
namespaces** (D3 — *not* a redesign; the earlier wording here contradicted D3's v3
downgrade); node labels and taints (D7 — taint DONE, resource requests still open).

### Phase 2 — Canaries, then ONE pilot
Synthetic DNS/ingress/storage/secret/GPU canaries **first**. Then **one** genuinely
low-dependency production service chosen from the P0.5 matrix. v1's three "stateless"
picks (flaresolverr, wrapperr, overseerr) are not independent — they participate in
application relationships. **Exit:** 48h clean, docker copy stopped but not deleted.

### Phase 3 — Bulk migration in dependency groups
Stateless leaves, then hostPath applications, grouped per the matrix.
**This phase dominates effort.**

### Phase 4 — Hard cases
GPU (verify a real hardware transcode, not device presence); VPN sidecar
(`network_mode: service:gluetun` → shared-netns Pod; preserve the `xtables-nft-multi`
fix since NixOS ships no legacy x_tables — `containers.nix:155`; verify the kill-switch
**fails closed** and that the pod cannot bypass the tunnel via IPv6 or cluster DNS);
palworld. (**`model-service` removed from this phase — parked with PinCollector, §0b.**)
Do not translate `privileged: true` mechanically — test whether
`/dev/net/tun` + `NET_ADMIN` suffices, drop other capabilities, disable service-account
token mounting.

### Phase 5 — Database bundles
**Two** PostgreSQL clusters as application+DB groups — `nextcloud-db` and
`immich-postgres14`. (`infra-postgres-1` is parked with PinCollector, §0b.) **After**
everything above is
exercised. Highest irreversible-write risk.

### Phase 6 — Ingress cutover, Docker decommission, cleanup
Move `:80`/`:443` to k3s traefik — this requires changing ServiceLB labels, firewalling,
traefik placement and `externalTrafficPolicy: Local` assumptions, not merely "moving
ports". 7-day Docker-off soak; keep compose files and images 30 days; only then remove
`virtualisation.docker`. Revisit Flux.

---

## 4. Effort

| Phase | Engineering | Calendar gate |
|---|---:|---|
| Phase 0 | ~~3–7 days + osgiliath build~~ **DONE** | restore drill passed 2026-08-06 |
| Phase 1 | 1–2 weeks | backup + restore proof |
| Phase 2 | 2–4 days | 48h soak |
| Phase 3 | 2–4 weeks | per-group soak |
| Phase 4 | 1–3 weeks | highest uncertainty |
| Phase 5 | 1–2 weeks | write freeze + restore validation |
| Phase 6 | 2–4 days + 7d soak | |

**~6–12 engineer-weeks.** Phase 3 dominates known labour; Phase 4 dominates uncertainty.

---

## 5. Acceptance

1. All migrated services Running/Ready.
2. Every hostname from the P0.5/D3 inventory answers with valid TLS, verified by an
   automated diff against the pre-migration list.
3. Plex and Jellyfin perform a real hardware transcode.
4. deluge-vpn's kill-switch verifiably **fails closed** when the tunnel drops.
5. Immich, Nextcloud, Plex and Jellyfin each log in and show real pre-existing data.
6. **A k8s-aware backup with database dumps runs and is restore-tested BEFORE the first
   stateful migration.** (v1 deferred this to the end — too late.)
7. Docker stopped on minas with zero service impact for 7 days.
8. No plaintext credentials on disk **and** k8s secret encryption at rest enabled.
9. ~~Control-plane failure drill: kill one server, cluster stays writable~~ — **debris
   from the abandoned three-server design; impossible on single-server SQLite.** Replaced
   by, and ALREADY SATISFIED as of 2026-08-06: a full control-plane restore drill —
   offsite snapshot restored in an isolated VM with credentials derived independently of
   pelargir, verified by CA fingerprint and Secret content matching live, not by object
   counts. See P0.3.
