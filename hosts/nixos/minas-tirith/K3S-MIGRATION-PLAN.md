# minas-tirith: Docker Compose → k3s migration plan (v3)

**Status:** v3, 2026-08-06. v2 was produced after an adversarial cross-model review
(gpt-5.6-sol, high effort, 848k tokens, 541s; verdict on v1: *"sound but needs specific
changes first"* — preserved as `K3S-MIGRATION-REVIEW.md`). v3 corrects three items in v2
that were verified against the live cluster rather than inferred.

**Owner decisions**
- Migrate ALL services to k3s (not the oci-containers alternative).
- **`osgiliath` is the third server node.** Its deployment was already queued behind
  minas; it is now a **prerequisite**, not a follow-up.
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
- `pelargir` — Pi 5, **single server, sqlite datastore**, k3s v1.35.6+k3s1,
  **untainted and schedulable**, carries
  `--node-label svccontroller.k3s.cattle.io/enablelb=true` (`hosts/nixos/pelargir/k3s.nix:19`)
- `minas-tirith` — agent. 32 threads, 125 GB RAM, RTX 2080 (driver 595.71.05),
  containerd 2.2.5-k3s2
- `osgiliath` — **configured but NOT deployed.** In the flake as **`x86_64-linux`**;
  full config exists (`disko.nix`, `k3s.nix`, `INSTALL-RUNBOOK.md`); currently
  `role = "agent"` with taint `osgiliath.saldivar.io/workloads=true:NoSchedule`
  (`hosts/nixos/osgiliath/k3s.nix:22`). Not on the tailnet, not in the cluster.
- k8s workloads on minas: **1** (`svclb-traefik`). Docker on minas: **35** containers —
  all real work happens there.
- Flux: not deployed.
- **cert-manager stack is healthy and broader than v1/v2 assumed** (verified 2026-08-06):
  ClusterIssuer `letsencrypt` Ready with `dnsZones: [saldivar.io]`; certs
  `pelargir-wildcard` (expires 2026-11-03) and `osgiliath-home-assistant` both Ready;
  reflector running in `kube-system`. Existing namespaces: `cert-manager`, `default`,
  `home`, `kube-node-lease`, `kube-public`, `kube-system`, `osgiliath`.

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

### D2 — GPU: **NVIDIA device plugin**, not CDI · REVERSED
v1 chose CDI because it is proven under Docker. That proves the host toolkit, not the
Kubernetes device-request path — and three consumers (plex, jellyfin, model-service)
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

### D8 — Control plane: **three servers, embedded etcd** · NEW, blocking
`pelargir` + `minas-tirith` + `osgiliath`. Two members give no fault tolerance, so three
is the minimum; the owner designated osgiliath, whose deployment was already queued.

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

## 3. Phases

### Phase 0 — Hard prerequisites (ALL blocking)
- **P0.1 Deploy osgiliath.** Config exists; the host does not. Resolve D8's wifi question first.
- **P0.2 Convert to 3-server embedded etcd** (pelargir, minas, osgiliath); flip roles agent→server.
- **P0.3 etcd snapshots to a failure-independent destination + a proven restore drill.** Today pelargir's backup copies `/var/lib/rancher/k3s/storage` but **not the datastore** (`hosts/nixos/pelargir/backup.nix:63`) — the control plane is currently unrecoverable.
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
- **P0.9 Image path for `pin-collector-model-service:local`** — a local build with no registry; k3s cannot pull it.
- **P0.10 35-row migration ledger.** v1's inventory contradicted itself: `qbittorrent-books` pinned but in no phase; `shelfmark`/`readmeabook` pinned then classed stateless; `komga`/`prowlarr` in the hostPath tier but absent from the pinned list. Reconcile against the live host.
- **P0.11 Commit the 2026-08-06 compose fixes**, plus a fresh backup + ZFS snapshot as the rollback point.

### Phase 1 — Foundations
Namespaces; NVIDIA device plugin (D2); secret pipeline + encryption at rest (D4); cert
and reflector redesign (D3); node labels and taints (D7).

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
palworld; model-service. Do not translate `privileged: true` mechanically — test whether
`/dev/net/tun` + `NET_ADMIN` suffices, drop other capabilities, disable service-account
token mounting.

### Phase 5 — Database bundles
All three PostgreSQL clusters as application+DB groups, **after** everything above is
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
| Phase 0 | 3–7 days + osgiliath build | failure/restore soak |
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
9. Control-plane failure drill: kill one server, cluster stays writable; restore etcd
   from snapshot into a scratch cluster successfully.
