# Codex review — K3S-PHASE1-PLAN.md (round 3)

Model gpt-5.6-sol (seat3) · effort high · 576s · 1,453,088 tokens · 2026-08-06

**Verdict: executable only after named changes — do not execute as written.**

     1	# minas-tirith: Docker Compose → k3s migration plan (v3)
     2	
     3	**Status:** v3, 2026-08-06. v2 was produced after an adversarial cross-model review
     4	(gpt-5.6-sol, high effort, 848k tokens, 541s; verdict on v1: *"sound but needs specific
     5	changes first"* — preserved as `K3S-MIGRATION-REVIEW.md`). v3 corrects three items in v2
     6	that were verified against the live cluster rather than inferred.
     7	
     8	**Owner decisions**
     9	- Migrate ALL services to k3s (not the oci-containers alternative).
    10	- **`osgiliath` is the third server node.** Its deployment was already queued behind
    11	  minas; it is now a **prerequisite**, not a follow-up.
    12	- **Jellyfin backups must have zero degradation** — accepted the brief stop, rejected a
    13	  permanently-degraded dump.
    14	
    15	---
    16	
    17	## v2 → v3: corrections from checking the live cluster
    18	
    19	All three were cases of asserting a problem that measurement did not support. Recorded
    20	because two of them made this plan look harder than it is.
    21	
    22	| v2 said | Measured reality | Effect |
    23	|---|---|---|
    24	| D8: osgiliath's `wifi.nix` makes it a poor etcd member | `wifi.nix:1` documents it as *"a lower-priority recovery path"*; primary is MAC-matched `10-ethernet`, wifi is `20-wifi-later` at a higher metric | D8 **softened** — the only requirement is that osgiliath is physically wired. Keep `wifi.nix`. |
    25	| D3: the cert/reflection stack needs redesigning, and the CF token may lack `Zone:Zone:Read` | The `letsencrypt` ClusterIssuer is **Ready** and its solver selector is `dnsZones: [saldivar.io]` — the **whole zone**. Two certs are already issued via DNS-01 (`pelargir-wildcard`, `osgiliath-home-assistant`), so the token demonstrably works. Reflector is deployed. | D3 **downgraded** from "redesign" to "add a Certificate object + widen reflector's namespace list". No new issuer or token. |
    26	| Jellyfin's dump is permanently degraded | Fixed and verified on a real run: `sqlite backup: …library.db (8 tables)` + `restarted jellyfin after consistent dump`, run `Result=success`, **no degraded marker** | Closed. See P0.6. |
    27	
    28	---
    29	
    30	## v1 → v2: what changed and why
    31	
    32	The review found four factual errors and several structural gaps. All corrected here.
    33	
    34	| v1 claim | Reality | Evidence |
    35	|---|---|---|
    36	| A `*.saldivar.io` cert exists and reflector propagates it | Cert covers **`ha-pelargir.saldivar.io`, `pelargir.saldivar.io`, `*.pelargir.saldivar.io`** only; reflector is scoped to the **`home`** namespace alone | `hosts/nixos/pelargir/manifests/ingress.yaml:247` and `:241`, both re-verified directly |
    37	| "101 ingress routes" are the bulk of the work | 101 counts **label keys**; a router carries several. ~30 real hostnames. The bulk is dependency-aware per-service migration | review, §Phase ordering |
    38	| Add minas as a 2nd server for HA | A 2-member etcd needs **both** for quorum → zero fault tolerance. Needs **three** | review Q1 |
    39	| Acceptance: "no plaintext credentials on disk" | False — Secret values sit unencrypted in the k3s datastore unless encryption-at-rest is enabled | review Q4 |
    40	
    41	New sections addressing gaps v1 missed entirely: service discovery (**D9**),
    42	transactional rollback (**D10**), scheduling isolation (**D7**), three-server control
    43	plane (**D8**), and Kubernetes-aware backup/monitoring (**P0.6**).
    44	
    45	---
    46	
    47	## 0. Ground truth (measured, not assumed)
    48	
    49	| | |
    50	|---|---|
    51	| Services declared across 6 compose projects | 67 |
    52	| Services actually running | **35** |
    53	| Data-pinned to minas (`/storage*` bind mounts) | **16** |
    54	| Bind-mounts into `/storage*` / `/etc`+`/usr/local` | 31 / 31 |
    55	| `privileged: true` | 2 (`deluge-vpn`, `deluge-books`) |
    56	| `cap_add` / `NET_ADMIN` | 9 |
    57	| `network_mode:` | 7 (incl. `network_mode: service:gluetun`) |
    58	| `devices:` blocks (GPU/dri) | 5 |
    59	| `depends_on` / `healthcheck` | 5 / 11 |
    60	| traefik router **label keys** (≈30 distinct hostnames) | 101 |
    61	
    62	**Cluster today**
    63	- `pelargir` — Pi 5, **single server, sqlite datastore**, k3s v1.35.6+k3s1,
    64	  **untainted and schedulable**, carries
    65	  `--node-label svccontroller.k3s.cattle.io/enablelb=true` (`hosts/nixos/pelargir/k3s.nix:19`)
    66	- `minas-tirith` — agent. 32 threads, 125 GB RAM, RTX 2080 (driver 595.71.05),
    67	  containerd 2.2.5-k3s2
    68	- `osgiliath` — **configured but NOT deployed.** In the flake as **`x86_64-linux`**;
    69	  full config exists (`disko.nix`, `k3s.nix`, `INSTALL-RUNBOOK.md`); currently
    70	  `role = "agent"` with taint `osgiliath.saldivar.io/workloads=true:NoSchedule`
    71	  (`hosts/nixos/osgiliath/k3s.nix:22`). Not on the tailnet, not in the cluster.
    72	- k8s workloads on minas: **1** (`svclb-traefik`). Docker on minas: **35** containers —
    73	  all real work happens there.
    74	- Flux: not deployed.
    75	- **cert-manager stack is healthy and broader than v1/v2 assumed** (verified 2026-08-06):
    76	  ClusterIssuer `letsencrypt` Ready with `dnsZones: [saldivar.io]`; certs
    77	  `pelargir-wildcard` (expires 2026-11-03) and `osgiliath-home-assistant` both Ready;
    78	  reflector running in `kube-system`. Existing namespaces: `cert-manager`, `default`,
    79	  `home`, `kube-node-lease`, `kube-public`, `kube-system`, `osgiliath`.
    80	
    81	---
    82	
    83	## 0b. Backlog — parked, not forgotten
    84	
    85	**PinCollector (`~/PinCollector/infra`) — STOPPED 2026-08-06, owner decision.**
    86	`infra-api-1`, `infra-minio-1` and `infra-postgres-1` were stopped and are parked
    87	pending a separate decision about where PinCollector lives. `model-service` was
    88	already not running.
    89	
    90	- Stopped **by container name, not `docker compose stop`** — `~/PinCollector/infra`
    91	  and `~/git/docker/infra` share the compose project name `infra`, so a compose
    92	  stop in one can take the other's containers (traefik2) with it. Verified
    93	  traefik2 stayed up.
    94	- `restart=unless-stopped`, so they stay down across reboots without further action.
    95	- Data intact and untouched on ZFS: `storage2/pincollector/postgres` (18 MB),
    96	  `storage2/pincollector/minio`, `storage/pincollector/minio`.
    97	- Removed from the heartbeat's critical-services list and the running-count
    98	  threshold lowered 35 → 28, because alerting on a state you chose is how an
    99	  alert becomes noise. **Re-add both when the stack returns.**
   100	- Brings P0.9 (registry for the two `:local` images) with it — see P0.9.
   101	
   102	Also parked: the **Nextcloud 28 → 34 upgrade ladder** (six majors, its own
   103	project), and the **`wolf` gameserver**, which uses `runtime: nvidia` and would
   104	need the same CDI conversion as plex/jellyfin if ever started.
   105	
   106	---
   107	
   108	## 1. Non-goals
   109	
   110	1. Do not migrate the 14 non-running gameservers (15 declared, only `palworld` ran).
   111	2. Do not do the Nextcloud 28→34 ladder here. Migrate **at 28**; upgrade separately.
   112	3. No Flux in phases 0–5. Plain manifests first (owner preference); revisit in Phase 6.
   113	4. Do not delete `/storage2/backup-2026-07-30` (298 GB) until every service is verified.
   114	
   115	---
   116	
   117	## 2. Decisions
   118	
   119	### D1 — Storage: `hostPath` + hostname `nodeSelector` · CONFIRMED
   120	Data physically exists only on minas; no abstraction changes that.
   121	**Additions required by review:** `hostPath.type: Directory`, never
   122	`DirectoryOrCreate` — an auto-created empty mountpoint is a *demonstrated* destructive
   123	failure mode on this host (`containers.nix:62` already gates Docker on ZFS readiness;
   124	k3s has no equivalent). Select on **hostname**, not a convenience `storage=zfs` label
   125	(v1 was inconsistent). `replicas: 1` + `strategy: Recreate` for every single-writer
   126	workload. **No `fsGroup`** that would recursively chown the media tree. New database
   127	PVCs on `local-path` must also be pinned to minas, capacity-checked, backed up, and
   128	given deletion protection — `local-path` is not automatically safe.
   129	
   130	### D2 — GPU: **NVIDIA device plugin**, not CDI · REVERSED
   131	v1 chose CDI because it is proven under Docker. That proves the host toolkit, not the
   132	Kubernetes device-request path — and three consumers (plex, jellyfin, model-service)
   133	sharing one RTX 2080 via `nvidia.com/gpu=all` leaves the scheduler blind. Deploy the
   134	plugin, request the extended resource, and choose **explicitly** between exclusive
   135	allocation and configured time-slicing, documenting that time-slicing gives scheduling
   136	accounting but **not** memory or fault isolation.
   137	
   138	### D3 — Ingress/TLS: add a Certificate, widen reflector · DOWNGRADED in v3
   139	v1 assumed a `*.saldivar.io` cert that does not exist. v2 over-corrected into "redesign
   140	the cert stack". Measurement says the foundation is **already sound**:
   141	
   142	- ClusterIssuer `letsencrypt` is **Ready**, and its solver selector is
   143	  `dnsZones: [saldivar.io]` — the **entire zone**, not just pelargir names
   144	  (`hosts/nixos/pelargir/manifests/ingress.yaml:219-227`).
   145	- Two certificates are already issued through it via DNS-01 (`pelargir-wildcard`,
   146	  `osgiliath-home-assistant`), which **proves the Cloudflare token has sufficient
   147	  scope** — the long-standing `Zone:Zone:Read` worry is settled.
   148	- Reflector is deployed and running in `kube-system`.
   149	
   150	What is actually narrow is only (a) the existing **Certificate object**, scoped to
   151	`ha-pelargir.saldivar.io`, `pelargir.saldivar.io`, `*.pelargir.saldivar.io`
   152	(`ingress.yaml:247`), and (b) reflector's `reflection-allowed-namespaces` /
   153	`reflection-auto-namespaces`, both set to `home` only (`ingress.yaml:241`).
   154	
   155	**Deliverable (Phase 1):** one additional Certificate covering the ~30 media hostnames,
   156	plus reflector's namespace list extended to every namespace that terminates TLS.
   157	**Constraint:** a `*.saldivar.io` SAN does **not** cover `admin.pin.saldivar.io` — two
   158	labels deep — so that needs its own SAN or a `*.pin.saldivar.io` alongside.
   159	**Do not issue before the namespace layout is fixed** (P0.5/P0.10): issuing production
   160	certs for hostnames nothing serves burns Let's Encrypt rate limit. Use LE **staging**
   161	for first issuance, per `INSTALL-RUNBOOK.md`.
   162	
   163	Route conversion is unchanged and still real work: inventory **routers, middleware
   164	chains, priorities, entrypoints, TLS options, backend scheme/port, websockets,
   165	redirects, auth and allowlists** — not label counts. Explicitly cover non-HTTP exposure
   166	(game server UDP/TCP, VPN/downloader ports, media discovery); HTTP IngressRoutes cannot
   167	replace all compose port publishing.
   168	
   169	### D4 — Secrets: sops-nix renders Secret manifests · CONFIRMED, spec expanded
   170	Valid because Nix activation is a legitimate declarative deploy mechanism, not merely
   171	because Flux is absent — `hosts/nixos/pelargir/secrets.nix:51` already does this.
   172	**What v1 omitted: who decrypts and who applies.** `secrets/minas-tirith.yaml` is
   173	decryptable by minas, but minas is an agent with no deploy credential (`.sops.yaml:47`).
   174	Either render application secrets on a server recipient or issue a tightly scoped deploy
   175	identity. Apply directly from `/run`; do **not** copy plaintext manifests to persistent
   176	storage. Handle rotation, pruning, and escaping of arbitrary `.env` values — prefer
   177	generated JSON/base64 over interpolating secrets into quoted YAML. **Enable Kubernetes
   178	secret encryption at rest.** Delete the plaintext `.env` files and add a `*.env` rule to
   179	that repo's `.gitignore` (absent today — `git add -A` would commit credentials).
   180	
   181	### D5 — Cutover unit is the **dependency group**, not the service · REVISED
   182	Stateless leaves may move alone; tightly coupled components and application/database
   183	pairs move together. **At no point may both copies write the same state.**
   184	
   185	### D6 — Databases: dump/restore · CONFIRMED, tightened
   186	Use a **final dump taken after stopping writers**, not the preceding nightly dump.
   187	Restore into the same PostgreSQL/extension family (Immich needs pgvector —
   188	`RESTORE-RUNBOOK.md:277`), fail on SQL errors, validate row counts, and coordinate
   189	application data with the database.
   190	
   191	### D7 — Scheduling isolation · NEW
   192	Pelargir is schedulable and untainted, and with no requests everything is BestEffort, so
   193	the scheduler cannot see the 125 GB/32-thread vs Pi disparity. Before the pilot: **taint
   194	pelargir** against migrated application workloads (osgiliath's `k3s.nix:22` shows the
   195	taint-by-default pattern to copy); **audit every image for amd64/arm64** — pelargir is
   196	ARM while minas and osgiliath are `x86_64-linux`, so amd64-only images must never land
   197	on the Pi; add realistic CPU/memory/ephemeral-storage requests derived from Docker
   198	observations; reserve headroom for k3s, Traefik, CoreDNS, storage and backup.
   199	
   200	### D8 — Control plane: **three servers, embedded etcd** · NEW, blocking
   201	`pelargir` + `minas-tirith` + `osgiliath`. Two members give no fault tolerance, so three
   202	is the minimum; the owner designated osgiliath, whose deployment was already queued.
   203	
   204	**v3 correction:** v2 flagged osgiliath's `wifi.nix` as disqualifying. It is not.
   205	`wifi.nix:1` documents it as *"a lower-priority recovery path"* — the primary is
   206	MAC-matched `10-ethernet`, and wifi is `20-wifi-later` carrying a higher DHCP metric, so
   207	it is a fallback that only takes over if the wire is gone. Keep the file. The residual
   208	requirement is operational, not architectural: **osgiliath must be physically wired**,
   209	because an etcd member that fails over to wifi and flaps costs quorum. Verify the link
   210	before promoting it to server, and treat "osgiliath running on wifi" as an alert
   211	condition rather than a supported steady state.
   212	
   213	Also note the arch split this creates: pelargir is ARM, minas and osgiliath are
   214	`x86_64-linux`. That is an advantage — amd64-only images have somewhere to go besides
   215	minas — but it makes D7's architecture audit mandatory rather than optional.
   216	
   217	### D9 — Service discovery must be designed before anything moves · NEW
   218	Containers today resolve each other by name on shared docker bridges (`traefik-net`,
   219	`plex-net`, `books-net`, `nextcloud-net`, `s3-net`, `infra_default`). Kubernetes does not
   220	preserve this, and **during coexistence Docker cannot resolve k8s Service names and k8s
   221	pods cannot resolve docker aliases.**
   222	**Deliverable:** a before/after matrix — caller, current hostname/network/port,
   223	transition endpoint, final Service DNS name, protocol, auth, health semantics — for
   224	every cross-service edge. Notably the `*arr` suite ↔ download clients ↔ indexers, and
   225	every app ↔ database/Redis/MinIO edge. Cross-namespace callers need
   226	`service.namespace.svc`. Internal traffic must **not** be routed through public ingress
   227	as an accidental substitute for discovery.
   228	*Without this, the pilot passes TLS while the applications silently stop talking.*
   229	
   230	### D10 — Transactional rollback per stateful cutover · NEW
   231	"Stop the docker copy" is not a rollback plan. Each stateful cutover defines:
   232	1. quiesce all writers; 2. record a final dump/snapshot identifier **and image digest**;
   233	3. start exactly one k8s writer; 4. state whether rollback preserves k8s-era writes
   234	(reverse dump/sync) or discards to the recorded point; 5. **prove the old image can read
   235	the post-cutover schema** before allowing reverse start. Applications that migrate SQLite
   236	schemas can render the old image unusable even against the same hostPath.
   237	
   238	### D11 — Compose semantics that do not survive translation · NEW
   239	`depends_on` (5) and healthchecks (11) have no Kubernetes equivalent that waits.
   240	Applications must tolerate retry, and probes must be classified correctly as
   241	startup/readiness/liveness — copying a compose healthcheck into an aggressive liveness
   242	probe creates destructive restart loops. Also inventory UIDs/GIDs (services here run as
   243	911 and 1000 — `RESTORE-RUNBOOK.md:172`), supplemental groups, `/dev/shm`, timezone
   244	mounts, sysctls, ulimits, stop signals and grace periods.
   245	
   246	---
   247	
   248	## 3. Phases
   249	
   250	### Phase 0 — Hard prerequisites (ALL blocking)
   251	- **P0.1 Deploy osgiliath.** Config exists; the host does not. Resolve D8's wifi question first.
   252	- **P0.2 Convert to 3-server embedded etcd** (pelargir, minas, osgiliath); flip roles agent→server.
   253	- **P0.3 etcd snapshots to a failure-independent destination + a proven restore drill.** Today pelargir's backup copies `/var/lib/rancher/k3s/storage` but **not the datastore** (`hosts/nixos/pelargir/backup.nix:63`) — the control plane is currently unrecoverable.
   254	- **P0.4 Fix pelargir's offsite backup** (MINAS-PREP §2/§3: the restic timers fire at a destination that does not exist and skip *quietly*).
   255	- **P0.5 Service dependency matrix** (D9). Highest-value artifact in this plan.
   256	- **P0.6 Replace Docker-specific backup and monitoring.** `system.nix:235` backs up `/var/lib/docker/volumes`, discovers postgres only via Docker, and records `docker-unavailable` once Docker stops (`system.nix:289`); `monitoring.nix:171` counts Docker containers. Both go blind at cutover.
   257	  **The k8s replacement must preserve a behaviour added 2026-08-06:** the sqlite dump
   258	  now *stops the owning container for the duration of its own dump* and restarts it
   259	  immediately, with a trap so a crash cannot leave the service down and a `CRITICAL`
   260	  marker (not the soft `degraded` one) if a restart fails. This exists because jellyfin
   261	  holds `library.db` write-locked for its entire runtime — a busy timeout was tried and
   262	  still failed after waiting the full 61s. Owner requirement is **zero degradation**, so
   263	  the k8s version needs the same quiesce-then-dump semantics (a pod-level equivalent:
   264	  scale to 0, dump, scale back), not a naive file copy of a live SQLite database.
   265	- **P0.7 Scheduling isolation** (D7): taint pelargir, arch audit, resource requests.
   266	- **P0.8 Resolve the `:80`/`:443` overlap and design coexistence routing** — how an unchanged hostname reaches Docker traefik for one service and k3s traefik for another when both cannot own the same address:port. v1 was internally inconsistent (Phase 2 assumed k3s ingress while Phase 5 postponed the port move).
   267	- **P0.9 Image path for the local builds** — ~~blocking~~ **DEFERRED 2026-08-06 with PinCollector itself (see Backlog).** Recorded because the constraint is permanent, not because it gates anything now: `pin-collector-api:local` (682 MB) and `pin-collector-model-service:local` are built from source by `~/PinCollector/infra/docker-compose.yml` and exist ONLY in docker's image store. k3s runs its own containerd with a separate store — measured: 47 images visible to docker, **0** pin-collector images visible to k3s. Any migration of those two needs a registry, a `ctr images import`, or an in-cluster build first.
   268	- **P0.10 35-row migration ledger.** v1's inventory contradicted itself: `qbittorrent-books` pinned but in no phase; `shelfmark`/`readmeabook` pinned then classed stateless; `komga`/`prowlarr` in the hostPath tier but absent from the pinned list. Reconcile against the live host.
   269	- ~~**P0.11 Commit the 2026-08-06 compose fixes**~~ — **DROPPED 2026-08-06 (owner decision).** The target is NixOS/k8s manifests in this repo, so `~/git/docker` is transitional and will not be versioned. Verified before dropping that the rollback baseline is still safe: `~/git/docker` sits inside the nightly backup at `/storage2/backup/minas-tirith/home/edgar/git/docker`, so today's fixes (readarr removal, CDI GPU, deluge nft entrypoints, the `~`→absolute path correction, komga TZ, nextcloud pin) survive loss of the root disk. They are backed up, just not version-controlled.
   270	  ⚠️ Consequence to accept knowingly: there is **no diff history** for that tree — its only commit is from 2023-03-23 and every stack directory is untracked. Recovery means restoring files, not reading a log.
   271	- **P0.11a Fresh backup + ZFS snapshot as the rollback point** — retained, since the rollback target still needs to be a known-good captured state.
   272	
   273	### Phase 1 — Foundations
   274	Namespaces; NVIDIA device plugin (D2); secret pipeline + encryption at rest (D4); cert
   275	and reflector redesign (D3); node labels and taints (D7).
   276	
   277	### Phase 2 — Canaries, then ONE pilot
   278	Synthetic DNS/ingress/storage/secret/GPU canaries **first**. Then **one** genuinely
   279	low-dependency production service chosen from the P0.5 matrix. v1's three "stateless"
   280	picks (flaresolverr, wrapperr, overseerr) are not independent — they participate in
   281	application relationships. **Exit:** 48h clean, docker copy stopped but not deleted.
   282	
   283	### Phase 3 — Bulk migration in dependency groups
   284	Stateless leaves, then hostPath applications, grouped per the matrix.
   285	**This phase dominates effort.**
   286	
   287	### Phase 4 — Hard cases
   288	GPU (verify a real hardware transcode, not device presence); VPN sidecar
   289	(`network_mode: service:gluetun` → shared-netns Pod; preserve the `xtables-nft-multi`
   290	fix since NixOS ships no legacy x_tables — `containers.nix:155`; verify the kill-switch
   291	**fails closed** and that the pod cannot bypass the tunnel via IPv6 or cluster DNS);
   292	palworld; model-service. Do not translate `privileged: true` mechanically — test whether
   293	`/dev/net/tun` + `NET_ADMIN` suffices, drop other capabilities, disable service-account
   294	token mounting.
   295	
   296	### Phase 5 — Database bundles
   297	All three PostgreSQL clusters as application+DB groups, **after** everything above is
   298	exercised. Highest irreversible-write risk.
   299	
   300	### Phase 6 — Ingress cutover, Docker decommission, cleanup
   301	Move `:80`/`:443` to k3s traefik — this requires changing ServiceLB labels, firewalling,
   302	traefik placement and `externalTrafficPolicy: Local` assumptions, not merely "moving
   303	ports". 7-day Docker-off soak; keep compose files and images 30 days; only then remove
   304	`virtualisation.docker`. Revisit Flux.
   305	
   306	---
   307	
   308	## 4. Effort
   309	
   310	| Phase | Engineering | Calendar gate |
   311	|---|---:|---|
   312	| Phase 0 | 3–7 days + osgiliath build | failure/restore soak |
   313	| Phase 1 | 1–2 weeks | backup + restore proof |
   314	| Phase 2 | 2–4 days | 48h soak |
   315	| Phase 3 | 2–4 weeks | per-group soak |
   316	| Phase 4 | 1–3 weeks | highest uncertainty |
   317	| Phase 5 | 1–2 weeks | write freeze + restore validation |
   318	| Phase 6 | 2–4 days + 7d soak | |
   319	
   320	**~6–12 engineer-weeks.** Phase 3 dominates known labour; Phase 4 dominates uncertainty.
   321	
   322	---
   323	
   324	## 5. Acceptance
   325	
   326	1. All migrated services Running/Ready.
   327	2. Every hostname from the P0.5/D3 inventory answers with valid TLS, verified by an
   328	   automated diff against the pre-migration list.
   329	3. Plex and Jellyfin perform a real hardware transcode.
   330	4. deluge-vpn's kill-switch verifiably **fails closed** when the tunnel drops.
   331	5. Immich, Nextcloud, Plex and Jellyfin each log in and show real pre-existing data.
   332	6. **A k8s-aware backup with database dumps runs and is restore-tested BEFORE the first
   333	   stateful migration.** (v1 deferred this to the end — too late.)
   334	7. Docker stopped on minas with zero service impact for 7 days.
   335	8. No plaintext credentials on disk **and** k8s secret encryption at rest enabled.
   336	9. Control-plane failure drill: kill one server, cluster stays writable; restore etcd
   337	   from snapshot into a scratch cluster successfully.
     1	# Codex review of K3S-MIGRATION-PLAN.md
     2	
     3	Model: gpt-5.6-sol (seat3) · effort high · 541s · 848,864 tokens · 2026-08-06
     4	
     5	## Verdict
     6	
     7	**Sound but needs specific changes first.** The migration goal and incremental approach are reasonable, but this draft is not executable safely. HA control-plane conversion, service-discovery/coexistence routing, transactional rollback, scheduling isolation, and Kubernetes-aware backup/monitoring must be designed and tested before Phase 2. Without those changes, the plan can produce apparently healthy pods while breaking application dependencies, losing rollback capability, or scheduling x86 workloads and databases onto the Pi.
     8	
     9	## Answers to Q1-Q6
    10	
    11	### Q1 — Is the Pi 5/sqlite control plane acceptable?
    12	
    13	**No. This must block production migration.** The minimum acceptable correction is **three k3s server nodes using embedded etcd**, not merely adding minas as a second server. A two-member etcd cluster still requires both members for quorum and provides no single-node failure tolerance. Pelargir, minas-tirith, and osgiliath are the obvious existing candidates; the latter two are currently agents pointing to pelargir ([minas k3s.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/k3s.nix:11>), [osgiliath k3s.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/osgiliath/k3s.nix:12>)). Convert the datastore, establish automated etcd snapshots to a failure-independent destination, and prove a restore before migrating real services.
    14	
    15	The recovery situation is worse than the draft implies: the repository says pelargir’s intended BMC lifeline still has a placeholder WireGuard peer and no handshake, while pelargir’s off-host backup is also not operational ([MINAS-PREP.md](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/pelargir/MINAS-PREP.md:12>)). A non-production canary may proceed, but Phase 2’s real services must not.
    16	
    17	### Q2 — Is hostPath plus nodeSelector right?
    18	
    19	**Yes, narrowly, for the existing 98 TB media tree.** A CSI layer cannot make host-local ZFS portable. Static local PVs would provide better object-level lifecycle and node affinity, but not materially better availability.
    20	
    21	Required additions: use `hostPath.type: Directory`, never `DirectoryOrCreate`; select the actual hostname as well as any convenience label; ensure k3s refuses storage-backed pods until both pools are imported and mounted; use `Recreate` for single-writer workloads; and avoid an `fsGroup` operation that recursively changes ownership across the media tree. Docker already has an explicit ZFS readiness gate because empty mountpoints are a demonstrated destructive failure mode ([containers.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/containers.nix:62>)); k3s currently has no equivalent.
    22	
    23	This answer does **not** endorse unconstrained `local-path` for new databases. Those PVCs must also be pinned to minas, capacity-checked, backed up, and given `Retain`-style deletion protection.
    24	
    25	### Q3 — Is CDI preferable to the NVIDIA device plugin?
    26	
    27	**No. Use the NVIDIA device plugin with an explicit sharing policy.** Docker CDI success proves the host toolkit and Docker integration, not Kubernetes scheduling or the separate k3s containerd path. Giving all three consumers `nvidia.com/gpu=all` leaves the scheduler blind and permits uncontrolled contention. Deploy the plugin, request the extended GPU resource, and choose explicitly between exclusive allocation and configured time-slicing. If time-slicing is selected, document that it supplies scheduling accounting but not memory or fault isolation.
    28	
    29	### Q4 — Is sops-nix rendering sound without Flux?
    30	
    31	**Yes, but not as currently specified.** The repository already demonstrates sops-nix rendering Kubernetes Secrets without Flux ([pelargir secrets.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/pelargir/secrets.nix:51>)). It is not a dead end.
    32	
    33	The missing design is who decrypts and who applies. `secrets/minas-tirith.yaml` is decryptable by minas, not pelargir ([.sops.yaml](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/.sops.yaml:47>)), while minas is only an agent and has no declared deploy credential. Either render dedicated application-secret files on a server recipient or give a tightly scoped deploy identity to the renderer. Apply directly from `/run`; do not copy plaintext manifests to persistent storage as the current pelargir pattern does ([manifests.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/pelargir/manifests.nix:62>)). Enable Kubernetes secret encryption at rest. Otherwise acceptance criterion 8—“no plaintext credentials remain on disk”—is false because Secret values remain in the k3s datastore even after `.env` deletion.
    34	
    35	### Q5 — Should databases precede GPU/VPN hard cases?
    36	
    37	**No. GPU and VPN validation should precede database cutovers.** They are technically difficult but more reversible. Database migration creates the largest irreversible-write and coordinated-downtime risk, so it should happen only after scheduling, storage, backup, service discovery, ingress, observability, and rollback have all been exercised.
    38	
    39	The three proposed pilot services are not a good first production move as a group. “Stateless” does not mean independent: Flaresolverr, Wrapperr, and Overseerr participate in application relationships. Begin with synthetic ingress/DNS/storage canaries and then one genuinely low-dependency service selected from the measured dependency graph.
    40	
    41	### Q6 — What is missing?
    42	
    43	The blocking omissions are ranked below. The largest is not manifest syntax; it is preserving the operational relationships Compose currently supplies implicitly.
    44	
    45	## Decisions D1-D6
    46	
    47	### D1 — hostPath plus nodeSelector
    48	
    49	**VERDICT: RIGHT.** It accurately reflects physical data locality and avoids pretending the workloads can reschedule. The decision is incomplete without ZFS readiness gating, `Directory` type checks, single-writer rollout policy, ownership safeguards, and explicit treatment of `local-path` databases. Phase 1 also inconsistently switches from hostname selection to a generic `storage=zfs` label ([plan](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/K3S-MIGRATION-PLAN.md:125>)); retain the hostname constraint.
    50	
    51	### D2 — CDI without the NVIDIA device plugin
    52	
    53	**VERDICT: WRONG.** The rationale optimizes away one small DaemonSet while discarding the scheduler information needed for three concurrent consumers. The verified Docker behavior is not proof of the intended Kubernetes device-request path. Use the plugin and define sharing deliberately.
    54	
    55	### D3 — IngressRoute plus existing wildcard certificate
    56	
    57	**VERDICT: WRONG AS WRITTEN.** Mechanical translation into Traefik CRDs is sensible, but both factual premises are wrong:
    58	
    59	- The existing certificate covers `ha-pelargir.saldivar.io`, `pelargir.saldivar.io`, and `*.pelargir.saldivar.io`, not `*.saldivar.io` ([ingress.yaml](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/pelargir/manifests/ingress.yaml:229>)).
    60	- Reflector is restricted to the `home` namespace, not the seven proposed namespaces ([ingress.yaml](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/pelargir/manifests/ingress.yaml:235>)).
    61	
    62	A new certificate/reflection design is required. `*.saldivar.io` also does not cover `admin.pin.saldivar.io`, which the host inventory explicitly contains ([containers.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/containers.nix:251>)).
    63	
    64	The measurement of 101 Traefik router **labels** is plausible; treating that as 101 routes or 101 IngressRoute objects is not. A router normally has several labels. The repository’s host list has roughly thirty names, which is consistent with 101 label keys but not necessarily 101 independent routes ([containers.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/containers.nix:221>)).
    65	
    66	### D4 — sops-nix-rendered Secrets
    67	
    68	**VERDICT: RIGHT FOR THE WRONG REASON.** This is valid because Nix activation can be a declarative deployment mechanism, not merely because Flux is absent. The current proposal omits apply authority, recipient ownership, rotation, deletion/pruning behavior, escaping arbitrary `.env` values, and datastore encryption. Prefer generated JSON or robust base64 rather than interpolating arbitrary secrets into quoted YAML.
    69	
    70	### D5 — strictly service-by-service coexistence
    71	
    72	**VERDICT: RIGHT FOR THE WRONG REASON.** Incremental coexistence is correct, but “service” is the wrong universal cutover unit. Stateless leaves may move individually; tightly connected components and stateful application/database pairs must move as dependency groups. At no point may both copies write the same state.
    73	
    74	The port conflict is only one part of coexistence. The plan never explains how an unchanged hostname reaches Docker Traefik for one service and k3s Traefik for another while both controllers cannot own the same address and ports. Phase 2 claims services will be live through k3s ingress, while Phase 5 postpones moving 80/443 to k3s. That requires an explicit per-host forwarding or DNS design.
    75	
    76	### D6 — PostgreSQL dump/restore
    77	
    78	**VERDICT: RIGHT.** Logical dump/restore is the safest migration boundary and avoids reusing incompatible PGDATA. “Never” is too categorical—cold copies and `pg_basebackup` can be valid—but dump/restore is the right choice here. Use a final dump after stopping writers, not merely the preceding nightly dump; restore into the same required PostgreSQL/extension family; fail on SQL errors; validate row counts; and coordinate application data with the database. The existing restore runbook correctly warns that Immich requires the proper extension-bearing image ([RESTORE-RUNBOOK.md](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/RESTORE-RUNBOOK.md:277>)).
    79	
    80	## Missing
    81	
    82	### 1. Control-plane recovery, not merely a risk acceptance paragraph
    83	
    84	R1 needs a committed prerequisite: three server nodes, embedded etcd, failure-domain tests, scheduled snapshots, off-node retention, and a restore drill. The existing pelargir backup copies `/var/lib/rancher/k3s/storage` but not the sqlite datastore ([backup.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/pelargir/backup.nix:63>)), and the repository says even that backup’s destination is not operational. “Accept and document” is not an acceptable branch for 35 production services.
    85	
    86	### 2. A service graph and dual-stack discovery design
    87	
    88	Compose currently provides DNS names and aliases only inside shared bridges. The restore runbook records nine networks, including `traefik-net`, `plex-net`, `books-net`, `nextcloud-net`, `s3-net`, and shared `infra_default` ([RESTORE-RUNBOOK.md](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/RESTORE-RUNBOOK.md:69>)). Kubernetes does not preserve those names automatically:
    89	
    90	- Every reachable workload needs a ClusterIP Service and declared ports.
    91	- Same-namespace callers may use `service`; cross-namespace callers must use `service.namespace.svc` or have their configuration rewritten.
    92	- Docker containers cannot resolve Kubernetes Service names, and Kubernetes pods cannot resolve Docker bridge aliases.
    93	- During coexistence, each cross-boundary edge needs a stable temporary endpoint—such as a Service/EndpointSlice pointing to an explicitly published Docker host port, and a stable NodePort/LAN endpoint for Docker-to-Kubernetes traffic.
    94	- Database, Redis, MinIO, downloader, indexer, and media-server traffic must not be routed through public ingress as an accidental substitute for service discovery.
    95	
    96	Create a before/after matrix of caller, current hostname/network/port, transition endpoint, final Service DNS name, protocol, auth, and health semantics. Migrate strongly connected groups together. Without this, the pilot can pass TLS while the applications silently cease talking to one another.
    97	
    98	### 3. Transactional rollback after the first Kubernetes write
    99	
   100	“Stop the Docker copy” is not a rollback plan. For each stateful cutover, specify:
   101	
   102	1. Stop or quiesce all writers.
   103	2. Record a final dump/snapshot/copy identifier and image digest.
   104	3. Start exactly one Kubernetes writer.
   105	4. Define whether rollback preserves Kubernetes-era writes through reverse dump/sync or explicitly discards them to the recorded point.
   106	5. Test that the old image can read the post-cutover schema before allowing reverse start.
   107	
   108	Applications that mutate SQLite schemas or configs can make the old Docker image unusable even when both point at the same hostPath. Nextcloud/DB, Immich/DB/media, and PinCollector/Postgres/MinIO need coordinated recovery points. The plan’s R5 names this risk but provides no procedure.
   109	
   110	### 4. Scheduling isolation, CPU/memory requests, architecture, and capacity
   111	
   112	Pelargir is schedulable and has no control-plane taint in its k3s configuration ([pelargir k3s.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/pelargir/k3s.nix:6>)). With no requests, workloads are BestEffort and the scheduler does not understand the 125 GB/32-thread versus Pi disparity. Stateless amd64-only images may also be sent to the ARM64 Pi and fail to pull.
   113	
   114	Before the pilot:
   115	
   116	- Taint pelargir against migrated application workloads and tolerate only explicitly approved system/home workloads.
   117	- Audit every image for amd64/arm64 support.
   118	- Add realistic CPU, memory, ephemeral-storage, and PVC requests based on Docker observations.
   119	- Use memory limits carefully; define database memory rather than allowing Pi-wide contention.
   120	- Reserve resources for k3s, Traefik, CoreDNS, storage, backup, and host operations.
   121	
   122	Osgiliath already demonstrates the correct “taint by default, explicit toleration” pattern ([osgiliath k3s.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/osgiliath/k3s.nix:18>)).
   123	
   124	### 5. Kubernetes-aware backup and monitoring
   125	
   126	The current minas backup copies `/var/lib/docker/volumes`, not `/var/lib/rancher/k3s/storage` ([system.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/system.nix:235>)). It discovers and dumps PostgreSQL only through Docker; after Docker is disabled it deliberately records `docker-unavailable` ([system.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/system.nix:289>)). Monitoring likewise counts Docker containers and checks Docker-specific names ([monitoring.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/monitoring.nix:171>)).
   127	
   128	Phase 1 must replace both before migrating state: Kubernetes-aware dumps, quiesced SQLite handling, local-path PVC backup, datastore snapshots, restore tests, pod/readiness/restart/ingress alerts, and backup-staleness alerts. Acceptance criterion 6 is too late; a tested backup is a prerequisite for the first stateful service.
   129	
   130	### 6. Correct ingress, TLS, and non-HTTP exposure inventory
   131	
   132	Mechanical conversion must inventory routers, services, middleware chains, priorities, entrypoints, TLS options, backend schemes/ports, websocket behavior, redirects, authentication, and allowlists—not count labels. Add explicit coverage for UDP/TCP and discovery ports such as games, VPN/downloader exposure, and media discovery; Traefik HTTP IngressRoutes cannot replace all Compose port publishing.
   133	
   134	The coexistence and final ServiceLB topology also need design. ServiceLB currently enters allow-list mode with only pelargir labeled ([pelargir k3s.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/pelargir/k3s.nix:14>)). Phase 5 cannot simply “move ports” without changing labels, firewalling, Traefik placement, and `externalTrafficPolicy: Local` assumptions.
   135	
   136	### 7. Storage placement and lifecycle for new state
   137	
   138	`local-path` does not mean safe or portable. The initial scheduling decision determines which node owns a database PVC. All three PostgreSQL clusters and other new state must be explicitly pinned to minas, included in backup, protected from accidental PVC deletion, capacity-monitored, and restored in a drill. Define whether each config remains at its current hostPath or is copied into a PVC; do not silently mix both patterns.
   139	
   140	### 8. Workload semantics lost in Compose translation
   141	
   142	The plan counts five `depends_on` and eleven healthchecks but never translates their meaning. Kubernetes Services do not wait for dependencies. Each application must tolerate retry, and probes must be classified correctly as startup, readiness, or liveness; copying a Compose healthcheck into an aggressive liveness probe can create destructive restart loops. Stateful hostPath applications need replicas `1` and `Recreate`, as the existing pelargir Home Assistant manifest already does for exclusive storage ([home-assistant.yaml](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/pelargir/manifests/home-assistant.yaml:19>)).
   143	
   144	Also inventory UIDs/GIDs, supplemental groups, `/dev/shm`, timezone mounts, sysctls, ulimits, stop signals/grace periods, and file ownership. The restore runbook specifically warns that service ownership differs ([RESTORE-RUNBOOK.md](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/RESTORE-RUNBOOK.md:172>)).
   145	
   146	### 9. Security boundaries for privileged and NET_ADMIN workloads
   147	
   148	Do not translate `privileged: true` mechanically. Test whether `/dev/net/tun` plus `NET_ADMIN` is sufficient, drop every other capability, disable service-account token mounting, apply seccomp, and isolate VPN pods from the API and other namespaces. Verify both leak directions: traffic must fail closed when the tunnel drops, and the pod must not bypass the tunnel through IPv6, cluster DNS, or another interface.
   149	
   150	### 10. Desired-state lifecycle and inventory reconciliation
   151	
   152	The plan needs a committed manifest location, validation, immutable image references, apply ordering, drift detection, and pruning/deletion behavior. Plain manifests are fine; unmanaged `kubectl apply` is not a lifecycle.
   153	
   154	The service inventory also does not reconcile cleanly. `qbittorrent-books` appears in the data-pinned list but nowhere in Phases 2–4. Conversely, `shelfmark` and `readmeabook` are declared data-pinned and then classified as “stateless + ingress only,” while `komga` and `prowlarr` appear in the hostPath tier despite not being in the measured pinned list ([plan inventory](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/K3S-MIGRATION-PLAN.md:28>), [phase tiers](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/K3S-MIGRATION-PLAN.md:133>)). Require a 35-row migration ledger before work begins, including the retiring Docker Traefik replacement.
   155	
   156	## Phase ordering and effort
   157	
   158	Recommended order:
   159	
   160	1. HA control plane, datastore backup/restore, BMC recovery path.
   161	2. Dependency/exposure inventory, scheduling isolation, resources, secret pipeline, Kubernetes-aware backup/monitoring.
   162	3. Synthetic DNS, ingress, storage, secret, and GPU canaries.
   163	4. One genuinely low-dependency production pilot.
   164	5. Stateless leaves, then hostPath applications in dependency-aware groups.
   165	6. GPU and VPN hard cases.
   166	7. PostgreSQL-backed application bundles last.
   167	8. Ingress ownership cutover, Docker shutdown, soak, then cleanup.
   168	
   169	Estimated one-engineer effort:
   170	
   171	| Phase | Engineering effort | Calendar gates |
   172	|---|---:|---:|
   173	| Phase 0 corrected | 3–7 days | At least several days of failure/restore soak; hardware procurement would add calendar time |
   174	| Phase 1 corrected | 1–2 weeks | Backup and restore proof required |
   175	| Pilot | 2–4 days | Existing 48-hour soak is reasonable |
   176	| Bulk T1/T2 | 2–4 weeks | Per-group functional soak |
   177	| GPU/VPN hard cases | 1–3 weeks | Highest debugging uncertainty |
   178	| Database bundles | 1–2 weeks | Planned write freeze and restore validation |
   179	| Ingress/decommission | 2–4 days | Seven-day Docker-off soak |
   180	| Cleanup | 1–3 days | Keep rollback material for the stated 30 days |
   181	
   182	Overall order of magnitude: **roughly 6–12 engineer-weeks**, plus soak/retention time. **Phase 3—the dependency-aware bulk migration—dominates known labor.** GPU/VPN dominates uncertainty, but the repeated per-service discovery, state, backup, route, probe, and functional verification work across the bulk inventory is larger than the hard-case count. The assertion that “101 ingress routes is the bulk of the work” is therefore not supported.
   183	
   184	## Things the plan got right
   185	
   186	- It uses the measured running set rather than blindly migrating dormant Compose declarations.
   187	- It identifies the port collision and local-only image as genuine Phase 0 blockers.
   188	- It chooses logical PostgreSQL migration and functional verification rather than trusting `Running` state.
   189	- It requires real GPU transcoding, VPN kill-switch failure testing, a mechanical ingress comparison, and retention of the known-good Docker rollback material.
   190	- Its warning about legacy x_tables is consistent with the detailed host configuration, which records that legacy modules are unavailable and the container must use the nft backend ([containers.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-nzjp7ty8/run-1786053455-56756/hosts/nixos/minas-tirith/containers.nix:155>)).
     1	# minas-tirith → k3s migration ledger (P0.10)
     2	
     3	Generated **2026-08-06** from the LIVE host — `docker inspect` of all 35 running
     4	containers, registry manifest inspection for architecture, and a scan of each
     5	application's own SQLite store for dependencies that never appear in the environment.
     6	
     7	> Regenerate after ANY compose change; this was rebuilt once already because the
     8	> first capture predated the `~`→`/root` path fix and recorded four wrong paths.
     9	
    10	**Legend** — `PRIV` privileged · `CAP:` added capabilities · `NETNS` shares another
    11	container's netns (→ one multi-container Pod) · `DEV` host device · `LOCAL-BUILD`
    12	no registry, k3s **cannot pull it** · `AMD64` single-arch, must not land on the ARM
    13	control plane · `(appdb)` dependency found inside the app's own database.
    14	
    15	| Service | Proj | Arch | Pinned | Config hostPath | Ports | Ingress | Depends on | Special | ns |
    16	|---|---|---|---|---|---|---|---|---|---|
    17	| `audiobookshelf` | books | amd64+arm64 | **YES** | `/usr/local/etc/audiobookshelf` | - | listen.saldivar.io | - | - | books |
    18	| `flaresolverr-books` | books | amd64+arm64 |  | `-` | - | - | - | **NETNS** | books |
    19	| `gluetun` | books | amd64 |  | `-` | 6881 8080 | books-dl.saldivar.io | - | CAP DEV | books |
    20	| `kavita` | books | amd64+arm64 | **YES** | `/usr/local/etc/kavita` | - | books.saldivar.io | _(appdb)_ calibre plex | - | books |
    21	| `qbittorrent-books` | books | amd64+arm64 | **YES** | `/usr/local/etc/qbittorrent-books` | - | - | - | **NETNS** | books |
    22	| `readmeabook` | books | amd64+arm64 | **YES** | `/usr/local/etc/readmeabook/config` | - | bookrequests.saldivar.io | - | - | books |
    23	| `shelfmark` | books | amd64+arm64 | **YES** | `/home/edgar/git/docker/books/shelfmark/config` | - | requestbooks.saldivar.io | _(appdb)_ prowlarr | - | books |
    24	| `nextcloud` | cloud | amd64+arm64 | **YES** | `-` | - | drive.saldivar.io | nextcloud-db | - | cloud |
    25	| `nextcloud-db` | cloud | amd64+arm64 | **YES** | `-` | - | - | - | - | cloud |
    26	| `nextcloud-redis` | cloud | amd64+arm64 |  | `/usr/local/lib/docker-nextcloud-redis` | - | - | - | - | cloud |
    27	| `palworld-server` | gameservers | amd64+arm64 |  | `-` | 27015 8211 | - | - | - | games |
    28	| `immich` | immich | amd64 | **YES** | `/home/edgar/git/docker/immich/config` | 8080 | immich.saldivar.io | immich-postgres14 immich-redis | **AMD64** | photos |
    29	| `immich-postgres14` | immich | amd64+arm64 | **YES** | `-` | 5432 | - | - | - | photos |
    30	| `immich-redis` | immich | amd64+arm64 |  | `-` | 6379 | - | - | - | photos |
    31	| `infra-api-1` | infra | amd64 |  | `-` | 8000 | admin.pin.saldivar.io,pin.saldivar.io | minio:9000 postgres postgres:5432 | **LOCAL-BUILD** | infra |
    32	| `infra-minio-1` | infra | amd64+arm64 |  | `-` | 9000 9001 | - | - | - | infra |
    33	| `infra-postgres-1` | infra | amd64+arm64 |  | `-` | 5432 | - | - | - | infra |
    34	| `traefik2` | infra | amd64+arm64 |  | `-` | 443 80 | traefik.saldivar.io | - | - | infra |
    35	| `animearr` | media | amd64+arm64 | **YES** | `/storage/Media/Torrents;/home/edgar/docker-services/animearr/config` | 8989 | anime.saldivar.io | - | - | media |
    36	| `calibre` | media | amd64+arm64 | **YES** | `/etc/calibre/config` | 8080 8081 8181 | - | - | - | media |
    37	| `deluge-books` | media | amd64+arm64 | **YES** | `/storage/Media/Torrents;/usr/local/etc/deluge-books` | 48846 48946 58846 58946 8112 8118 | btbooks.saldivar.io | - | **PRIV** | media |
    38	| `deluge-vpn` | media | amd64+arm64 | **YES** | `/storage/Media/Torrents;/usr/local/etc/docker-deluge-vpn` | 58846 58946 8112 8118 | bt.saldivar.io | - | **PRIV** | media |
    39	| `flaresolverr` | media | amd64+arm64 |  | `-` | 8191 | - | - | - | media |
    40	| `jellyfin` | media | amd64+arm64 | **YES** | `/usr/local/etc/jellyfin/config` | 8096 8920 | jellyfin.saldivar.io | - | DEV | media |
    41	| `komga` | media | amd64+arm64 | **YES** | `/etc/komga/config;/storage/Media/manga` | 25600 | komga.saldivar.io | - | - | media |
    42	| `lidarr` | media | amd64+arm64 | **YES** | `/etc/lidarr/data` | 8686 | lidarr.saldivar.io | _(appdb)_ prowlarr deluge-vpn plex | - | media |
    43	| `maintainerr` | media | amd64+arm64 |  | `-` | - | maintainerr.saldivar.io | - | - | media |
    44	| `media-tautulli-1` | media | amd64+arm64 |  | `/usr/local/etc/tautulli` | - | tautulli.saldivar.io | _(appdb)_ plex | - | media |
    45	| `media-tracearr-1` | media | amd64+arm64 |  | `-` | 3000 | trace.saldivar.io | redis | - | media |
    46	| `overseerr` | media | amd64+arm64 |  | `/usr/local/etc/docker-overseer` | 5055 | overseer.saldivar.io,requests.saldivar.io | - | - | media |
    47	| `plex` | media | amd64+arm64 | **YES** | `/home/edgar/docker-services/plex/config` | 32400 | plex.saldivar.io | - | DEV | media |
    48	| `prowlarr` | media | amd64+arm64 |  | `/usr/local/etc/docker-prowlarr` | - | prowlarr.saldivar.io | _(appdb)_ animearr sonarr radarr lidarr readmeabook deluge-books flaresolverr plex | - | media |
    49	| `radarr` | media | amd64+arm64 | **YES** | `/home/edgar/docker-services/radarr/config` | 7878 | radarr.saldivar.io | - | - | media |
    50	| `sonarr` | media | amd64+arm64 | **YES** | `/home/edgar/docker-services/sonarr/config;/storage/Media/Torrents` | 8989 | sonarr.saldivar.io | - | - | media |
    51	| `wrapperr` | media | amd64+arm64 |  | `/etc/wrapper` | 8282 | stats.saldivar.io,wrapperr.saldivar.io | - | - | media |
    52	
    53	## What this changes in the plan
    54	
    55	- **GPU consumers are 2, not 3.** Only `plex` and `jellyfin` hold the GPU; it is
    56	  idle (1 MiB / 8192 MiB, 0%). `model-service` is not running. `immich` uses a
    57	  `-cuda` image but requests **no GPU at all** — pre-existing, likely unintended.
    58	- **Two local-build images, not one.** `pin-collector-api:local` as well as
    59	  `model-service`. Neither can be pulled by k3s → P0.9 is larger than stated.
    60	- **Architecture is nearly a non-issue.** Only `immich` is single-arch amd64;
    61	  everything else publishes arm64. Data locality, not arch, is the binding
    62	  constraint — 16 services are pinned to minas by `/storage*` mounts.
    63	- **The dependency graph is mostly invisible to env vars.** Only 7 edges come
    64	  from the environment; the `*arr` mesh lives inside application databases.

