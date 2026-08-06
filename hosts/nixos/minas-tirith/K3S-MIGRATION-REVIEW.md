# Codex review of K3S-MIGRATION-PLAN.md

Model: gpt-5.6-sol (seat3) · effort high · 541s · 848,864 tokens · 2026-08-06

## Verdict

**Sound but needs specific changes first.** The migration goal and incremental approach are reasonable, but this draft is not executable safely. HA control-plane conversion, service-discovery/coexistence routing, transactional rollback, scheduling isolation, and Kubernetes-aware backup/monitoring must be designed and tested before Phase 2. Without those changes, the plan can produce apparently healthy pods while breaking application dependencies, losing rollback capability, or scheduling x86 workloads and databases onto the Pi.

## Answers to Q1-Q6

### Q1 — Is the Pi 5/sqlite control plane acceptable?

**No. This must block production migration.** The minimum acceptable correction is **three k3s server nodes using embedded etcd**, not merely adding minas as a second server. A two-member etcd cluster still requires both members for quorum and provides no single-node failure tolerance. Pelargir, minas-tirith, and osgiliath are the obvious existing candidates; the latter two are currently agents pointing to pelargir ([minas k3s.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/k3s.nix:11>), [osgiliath k3s.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/osgiliath/k3s.nix:12>)). Convert the datastore, establish automated etcd snapshots to a failure-independent destination, and prove a restore before migrating real services.

The recovery situation is worse than the draft implies: the repository says pelargir’s intended BMC lifeline still has a placeholder WireGuard peer and no handshake, while pelargir’s off-host backup is also not operational ([MINAS-PREP.md](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/pelargir/MINAS-PREP.md:12>)). A non-production canary may proceed, but Phase 2’s real services must not.

### Q2 — Is hostPath plus nodeSelector right?

**Yes, narrowly, for the existing 98 TB media tree.** A CSI layer cannot make host-local ZFS portable. Static local PVs would provide better object-level lifecycle and node affinity, but not materially better availability.

Required additions: use `hostPath.type: Directory`, never `DirectoryOrCreate`; select the actual hostname as well as any convenience label; ensure k3s refuses storage-backed pods until both pools are imported and mounted; use `Recreate` for single-writer workloads; and avoid an `fsGroup` operation that recursively changes ownership across the media tree. Docker already has an explicit ZFS readiness gate because empty mountpoints are a demonstrated destructive failure mode ([containers.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/containers.nix:62>)); k3s currently has no equivalent.

This answer does **not** endorse unconstrained `local-path` for new databases. Those PVCs must also be pinned to minas, capacity-checked, backed up, and given `Retain`-style deletion protection.

### Q3 — Is CDI preferable to the NVIDIA device plugin?

**No. Use the NVIDIA device plugin with an explicit sharing policy.** Docker CDI success proves the host toolkit and Docker integration, not Kubernetes scheduling or the separate k3s containerd path. Giving all three consumers `nvidia.com/gpu=all` leaves the scheduler blind and permits uncontrolled contention. Deploy the plugin, request the extended GPU resource, and choose explicitly between exclusive allocation and configured time-slicing. If time-slicing is selected, document that it supplies scheduling accounting but not memory or fault isolation.

### Q4 — Is sops-nix rendering sound without Flux?

**Yes, but not as currently specified.** The repository already demonstrates sops-nix rendering Kubernetes Secrets without Flux ([pelargir secrets.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/pelargir/secrets.nix:51>)). It is not a dead end.

The missing design is who decrypts and who applies. `secrets/minas-tirith.yaml` is decryptable by minas, not pelargir ([.sops.yaml](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/.sops.yaml:47>)), while minas is only an agent and has no declared deploy credential. Either render dedicated application-secret files on a server recipient or give a tightly scoped deploy identity to the renderer. Apply directly from `/run`; do not copy plaintext manifests to persistent storage as the current pelargir pattern does ([manifests.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/pelargir/manifests.nix:62>)). Enable Kubernetes secret encryption at rest. Otherwise acceptance criterion 8—“no plaintext credentials remain on disk”—is false because Secret values remain in the k3s datastore even after `.env` deletion.

### Q5 — Should databases precede GPU/VPN hard cases?

**No. GPU and VPN validation should precede database cutovers.** They are technically difficult but more reversible. Database migration creates the largest irreversible-write and coordinated-downtime risk, so it should happen only after scheduling, storage, backup, service discovery, ingress, observability, and rollback have all been exercised.

The three proposed pilot services are not a good first production move as a group. “Stateless” does not mean independent: Flaresolverr, Wrapperr, and Overseerr participate in application relationships. Begin with synthetic ingress/DNS/storage canaries and then one genuinely low-dependency service selected from the measured dependency graph.

### Q6 — What is missing?

The blocking omissions are ranked below. The largest is not manifest syntax; it is preserving the operational relationships Compose currently supplies implicitly.

## Decisions D1-D6

### D1 — hostPath plus nodeSelector

**VERDICT: RIGHT.** It accurately reflects physical data locality and avoids pretending the workloads can reschedule. The decision is incomplete without ZFS readiness gating, `Directory` type checks, single-writer rollout policy, ownership safeguards, and explicit treatment of `local-path` databases. Phase 1 also inconsistently switches from hostname selection to a generic `storage=zfs` label ([plan](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/K3S-MIGRATION-PLAN.md:125>)); retain the hostname constraint.

### D2 — CDI without the NVIDIA device plugin

**VERDICT: WRONG.** The rationale optimizes away one small DaemonSet while discarding the scheduler information needed for three concurrent consumers. The verified Docker behavior is not proof of the intended Kubernetes device-request path. Use the plugin and define sharing deliberately.

### D3 — IngressRoute plus existing wildcard certificate

**VERDICT: WRONG AS WRITTEN.** Mechanical translation into Traefik CRDs is sensible, but both factual premises are wrong:

- The existing certificate covers `ha-pelargir.saldivar.io`, `pelargir.saldivar.io`, and `*.pelargir.saldivar.io`, not `*.saldivar.io` ([ingress.yaml](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/pelargir/manifests/ingress.yaml:229>)).
- Reflector is restricted to the `home` namespace, not the seven proposed namespaces ([ingress.yaml](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/pelargir/manifests/ingress.yaml:235>)).

A new certificate/reflection design is required. `*.saldivar.io` also does not cover `admin.pin.saldivar.io`, which the host inventory explicitly contains ([containers.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/containers.nix:251>)).

The measurement of 101 Traefik router **labels** is plausible; treating that as 101 routes or 101 IngressRoute objects is not. A router normally has several labels. The repository’s host list has roughly thirty names, which is consistent with 101 label keys but not necessarily 101 independent routes ([containers.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/containers.nix:221>)).

### D4 — sops-nix-rendered Secrets

**VERDICT: RIGHT FOR THE WRONG REASON.** This is valid because Nix activation can be a declarative deployment mechanism, not merely because Flux is absent. The current proposal omits apply authority, recipient ownership, rotation, deletion/pruning behavior, escaping arbitrary `.env` values, and datastore encryption. Prefer generated JSON or robust base64 rather than interpolating arbitrary secrets into quoted YAML.

### D5 — strictly service-by-service coexistence

**VERDICT: RIGHT FOR THE WRONG REASON.** Incremental coexistence is correct, but “service” is the wrong universal cutover unit. Stateless leaves may move individually; tightly connected components and stateful application/database pairs must move as dependency groups. At no point may both copies write the same state.

The port conflict is only one part of coexistence. The plan never explains how an unchanged hostname reaches Docker Traefik for one service and k3s Traefik for another while both controllers cannot own the same address and ports. Phase 2 claims services will be live through k3s ingress, while Phase 5 postpones moving 80/443 to k3s. That requires an explicit per-host forwarding or DNS design.

### D6 — PostgreSQL dump/restore

**VERDICT: RIGHT.** Logical dump/restore is the safest migration boundary and avoids reusing incompatible PGDATA. “Never” is too categorical—cold copies and `pg_basebackup` can be valid—but dump/restore is the right choice here. Use a final dump after stopping writers, not merely the preceding nightly dump; restore into the same required PostgreSQL/extension family; fail on SQL errors; validate row counts; and coordinate application data with the database. The existing restore runbook correctly warns that Immich requires the proper extension-bearing image ([RESTORE-RUNBOOK.md](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/RESTORE-RUNBOOK.md:277>)).

## Missing

### 1. Control-plane recovery, not merely a risk acceptance paragraph

R1 needs a committed prerequisite: three server nodes, embedded etcd, failure-domain tests, scheduled snapshots, off-node retention, and a restore drill. The existing pelargir backup copies `/var/lib/rancher/k3s/storage` but not the sqlite datastore ([backup.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/pelargir/backup.nix:63>)), and the repository says even that backup’s destination is not operational. “Accept and document” is not an acceptable branch for 35 production services.

### 2. A service graph and dual-stack discovery design

Compose currently provides DNS names and aliases only inside shared bridges. The restore runbook records nine networks, including `traefik-net`, `plex-net`, `books-net`, `nextcloud-net`, `s3-net`, and shared `infra_default` ([RESTORE-RUNBOOK.md](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/RESTORE-RUNBOOK.md:69>)). Kubernetes does not preserve those names automatically:

- Every reachable workload needs a ClusterIP Service and declared ports.
- Same-namespace callers may use `service`; cross-namespace callers must use `service.namespace.svc` or have their configuration rewritten.
- Docker containers cannot resolve Kubernetes Service names, and Kubernetes pods cannot resolve Docker bridge aliases.
- During coexistence, each cross-boundary edge needs a stable temporary endpoint—such as a Service/EndpointSlice pointing to an explicitly published Docker host port, and a stable NodePort/LAN endpoint for Docker-to-Kubernetes traffic.
- Database, Redis, MinIO, downloader, indexer, and media-server traffic must not be routed through public ingress as an accidental substitute for service discovery.

Create a before/after matrix of caller, current hostname/network/port, transition endpoint, final Service DNS name, protocol, auth, and health semantics. Migrate strongly connected groups together. Without this, the pilot can pass TLS while the applications silently cease talking to one another.

### 3. Transactional rollback after the first Kubernetes write

“Stop the Docker copy” is not a rollback plan. For each stateful cutover, specify:

1. Stop or quiesce all writers.
2. Record a final dump/snapshot/copy identifier and image digest.
3. Start exactly one Kubernetes writer.
4. Define whether rollback preserves Kubernetes-era writes through reverse dump/sync or explicitly discards them to the recorded point.
5. Test that the old image can read the post-cutover schema before allowing reverse start.

Applications that mutate SQLite schemas or configs can make the old Docker image unusable even when both point at the same hostPath. Nextcloud/DB, Immich/DB/media, and PinCollector/Postgres/MinIO need coordinated recovery points. The plan’s R5 names this risk but provides no procedure.

### 4. Scheduling isolation, CPU/memory requests, architecture, and capacity

Pelargir is schedulable and has no control-plane taint in its k3s configuration ([pelargir k3s.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/pelargir/k3s.nix:6>)). With no requests, workloads are BestEffort and the scheduler does not understand the 125 GB/32-thread versus Pi disparity. Stateless amd64-only images may also be sent to the ARM64 Pi and fail to pull.

Before the pilot:

- Taint pelargir against migrated application workloads and tolerate only explicitly approved system/home workloads.
- Audit every image for amd64/arm64 support.
- Add realistic CPU, memory, ephemeral-storage, and PVC requests based on Docker observations.
- Use memory limits carefully; define database memory rather than allowing Pi-wide contention.
- Reserve resources for k3s, Traefik, CoreDNS, storage, backup, and host operations.

Osgiliath already demonstrates the correct “taint by default, explicit toleration” pattern ([osgiliath k3s.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/osgiliath/k3s.nix:18>)).

### 5. Kubernetes-aware backup and monitoring

The current minas backup copies `/var/lib/docker/volumes`, not `/var/lib/rancher/k3s/storage` ([system.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/system.nix:235>)). It discovers and dumps PostgreSQL only through Docker; after Docker is disabled it deliberately records `docker-unavailable` ([system.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/system.nix:289>)). Monitoring likewise counts Docker containers and checks Docker-specific names ([monitoring.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/monitoring.nix:171>)).

Phase 1 must replace both before migrating state: Kubernetes-aware dumps, quiesced SQLite handling, local-path PVC backup, datastore snapshots, restore tests, pod/readiness/restart/ingress alerts, and backup-staleness alerts. Acceptance criterion 6 is too late; a tested backup is a prerequisite for the first stateful service.

### 6. Correct ingress, TLS, and non-HTTP exposure inventory

Mechanical conversion must inventory routers, services, middleware chains, priorities, entrypoints, TLS options, backend schemes/ports, websocket behavior, redirects, authentication, and allowlists—not count labels. Add explicit coverage for UDP/TCP and discovery ports such as games, VPN/downloader exposure, and media discovery; Traefik HTTP IngressRoutes cannot replace all Compose port publishing.

The coexistence and final ServiceLB topology also need design. ServiceLB currently enters allow-list mode with only pelargir labeled ([pelargir k3s.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/pelargir/k3s.nix:14>)). Phase 5 cannot simply “move ports” without changing labels, firewalling, Traefik placement, and `externalTrafficPolicy: Local` assumptions.

### 7. Storage placement and lifecycle for new state

`local-path` does not mean safe or portable. The initial scheduling decision determines which node owns a database PVC. All three PostgreSQL clusters and other new state must be explicitly pinned to minas, included in backup, protected from accidental PVC deletion, capacity-monitored, and restored in a drill. Define whether each config remains at its current hostPath or is copied into a PVC; do not silently mix both patterns.

### 8. Workload semantics lost in Compose translation

The plan counts five `depends_on` and eleven healthchecks but never translates their meaning. Kubernetes Services do not wait for dependencies. Each application must tolerate retry, and probes must be classified correctly as startup, readiness, or liveness; copying a Compose healthcheck into an aggressive liveness probe can create destructive restart loops. Stateful hostPath applications need replicas `1` and `Recreate`, as the existing pelargir Home Assistant manifest already does for exclusive storage ([home-assistant.yaml](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/pelargir/manifests/home-assistant.yaml:19>)).

Also inventory UIDs/GIDs, supplemental groups, `/dev/shm`, timezone mounts, sysctls, ulimits, stop signals/grace periods, and file ownership. The restore runbook specifically warns that service ownership differs ([RESTORE-RUNBOOK.md](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/RESTORE-RUNBOOK.md:172>)).

### 9. Security boundaries for privileged and NET_ADMIN workloads

Do not translate `privileged: true` mechanically. Test whether `/dev/net/tun` plus `NET_ADMIN` is sufficient, drop every other capability, disable service-account token mounting, apply seccomp, and isolate VPN pods from the API and other namespaces. Verify both leak directions: traffic must fail closed when the tunnel drops, and the pod must not bypass the tunnel through IPv6, cluster DNS, or another interface.

### 10. Desired-state lifecycle and inventory reconciliation

The plan needs a committed manifest location, validation, immutable image references, apply ordering, drift detection, and pruning/deletion behavior. Plain manifests are fine; unmanaged `kubectl apply` is not a lifecycle.

The service inventory also does not reconcile cleanly. `qbittorrent-books` appears in the data-pinned list but nowhere in Phases 2–4. Conversely, `shelfmark` and `readmeabook` are declared data-pinned and then classified as “stateless + ingress only,” while `komga` and `prowlarr` appear in the hostPath tier despite not being in the measured pinned list ([plan inventory](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/K3S-MIGRATION-PLAN.md:28>), [phase tiers](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/K3S-MIGRATION-PLAN.md:133>)). Require a 35-row migration ledger before work begins, including the retiring Docker Traefik replacement.

## Phase ordering and effort

Recommended order:

1. HA control plane, datastore backup/restore, BMC recovery path.
2. Dependency/exposure inventory, scheduling isolation, resources, secret pipeline, Kubernetes-aware backup/monitoring.
3. Synthetic DNS, ingress, storage, secret, and GPU canaries.
4. One genuinely low-dependency production pilot.
5. Stateless leaves, then hostPath applications in dependency-aware groups.
6. GPU and VPN hard cases.
7. PostgreSQL-backed application bundles last.
8. Ingress ownership cutover, Docker shutdown, soak, then cleanup.

Estimated one-engineer effort:

| Phase | Engineering effort | Calendar gates |
|---|---:|---:|
| Phase 0 corrected | 3–7 days | At least several days of failure/restore soak; hardware procurement would add calendar time |
| Phase 1 corrected | 1–2 weeks | Backup and restore proof required |
| Pilot | 2–4 days | Existing 48-hour soak is reasonable |
| Bulk T1/T2 | 2–4 weeks | Per-group functional soak |
| GPU/VPN hard cases | 1–3 weeks | Highest debugging uncertainty |
| Database bundles | 1–2 weeks | Planned write freeze and restore validation |
| Ingress/decommission | 2–4 days | Seven-day Docker-off soak |
| Cleanup | 1–3 days | Keep rollback material for the stated 30 days |

Overall order of magnitude: **roughly 6–12 engineer-weeks**, plus soak/retention time. **Phase 3—the dependency-aware bulk migration—dominates known labor.** GPU/VPN dominates uncertainty, but the repeated per-service discovery, state, backup, route, probe, and functional verification work across the bulk inventory is larger than the hard-case count. The assertion that “101 ingress routes is the bulk of the work” is therefore not supported.

## Things the plan got right

- It uses the measured running set rather than blindly migrating dormant Compose declarations.
- It identifies the port collision and local-only image as genuine Phase 0 blockers.
- It chooses logical PostgreSQL migration and functional verification rather than trusting `Running` state.
- It requires real GPU transcoding, VPN kill-switch failure testing, a mechanical ingress comparison, and retention of the known-good Docker rollback material.
- Its warning about legacy x_tables is consistent with the detailed host configuration, which records that legacy modules are unavailable and the container must use the nft backend ([containers.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/containers.nix:155>)).
