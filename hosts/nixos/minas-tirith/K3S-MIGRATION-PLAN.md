# minas-tirith: Docker Compose → k3s migration plan

**Status:** DRAFT for review. Authored 2026-08-06, immediately after minas-tirith was
rebuilt to NixOS 26.05 and its 35 containers restored from backup.

**Decision already made by the owner:** migrate ALL services to k3s (not the hybrid
oci-containers split that was also on the table). This document plans that.

---

## 0. Ground truth (measured on the live host, not assumed)

| | |
|---|---|
| Services declared across 6 compose projects | 67 |
| Services actually running | **35** |
| Services bind-mounting `/storage*` (data-pinned to minas) | **16** |
| Bind-mounts into `/storage*` | 31 |
| Bind-mounts into `/etc` or `/usr/local` | 31 |
| `privileged: true` | 2 (`deluge-vpn`, `deluge-books`) |
| `cap_add` / `NET_ADMIN` | 9 |
| `network_mode:` | 7 (incl. `network_mode: service:gluetun`) |
| `devices:` blocks (GPU/dri) | 5 |
| `depends_on` | 5 |
| `healthcheck` | 11 |
| **traefik router labels (ingress routes)** | **101** |

The 16 data-pinned services: `plex jellyfin sonarr radarr lidarr animearr immich
calibre kavita audiobookshelf shelfmark readmeabook qbittorrent-books deluge-vpn
deluge-books postgres14`.

**Cluster today**
- Control plane: `pelargir` — **Raspberry Pi 5, single server, sqlite datastore**, k3s v1.35.6+k3s1
- Agent: `minas-tirith` — 32 threads, 125 GB RAM, RTX 2080 (driver 595.71.05), containerd 2.2.5-k3s2
- Both joined over Tailscale (`tag:fleet`, no key expiry); flannel `10.42.0.0/24` via pelargir
- k8s workloads on minas today: **1** (`svclb-traefik`) — the cluster is effectively empty
- Docker on minas: separate containerd 29.6.2, 35 containers — this is where all real work happens
- cert-manager + reflector already on pelargir (CF DNS-01, wildcard cert)
- Flux: **not deployed.** `k3s-collective` / `doa-cluster-flux` repos were never deployed
- Secrets: 6 plaintext `.env` files, **mode 644**, containing CF DNS API token, PIA creds,
  JWT secrets, postgres passwords

**Known-live hazard:** docker-proxy holds `:80`/`:443` while the k3s `svclb-traefik`
speaker also runs on minas with `lb-tcp-80`/`lb-tcp-443`. Commit `7485e32` intended to
constrain ServiceLB to the control plane; it did not take effect for minas.

---

## 1. Explicit non-goals

1. **Do not migrate the 14 non-running gameservers.** The compose declares 15, only
   `palworld` ran. Migrating dormant services multiplies work for zero value.
2. **Do not do the Nextcloud 28→34 upgrade ladder as part of this.** It is a separate
   six-major-version project. Migrate Nextcloud **at 28**, upgrade later.
3. **Do not introduce Flux in phase 1.** Plain manifests first (the owner's stated
   preference); Flux is a later, separate step.
4. **Do not delete `/storage2/backup-2026-07-30`** (298 GB) until every migrated service
   is functionally verified.

---

## 2. Decisions this plan takes (flag if wrong)

### D1 — Storage: `hostPath` + `nodeSelector`, not a CSI/PV abstraction
The 16 data-pinned services need the ZFS pools mounted on minas. Proposal: `hostPath`
volumes plus `nodeSelector: kubernetes.io/hostname=minas-tirith`.
*Rationale:* the data physically exists only on minas; any abstraction still resolves to
the same node. `local-path-provisioner` (k3s default) is used only for NEW state
(databases), not for the media tree.
*Cost:* these pods cannot reschedule. Accepted — they could not anyway.

### D2 — GPU: CDI, not the nvidia device plugin
CDI already works on this host (`nvidia.com/gpu=all` verified against docker). k3s's
containerd supports CDI. Proposal: enable CDI in k3s containerd and request devices via
CDI rather than deploying `nvidia-device-plugin`.
*Alternative considered:* nvidia device plugin + `nvidia.com/gpu: 1` resource limits —
more idiomatic, gives scheduler awareness, but adds a DaemonSet and duplicates what CDI
already does on this box.

### D3 — Ingress: Traefik `IngressRoute` CRDs, reusing the existing wildcard cert
101 router labels become IngressRoute objects. cert-manager + reflector already issue and
propagate a wildcard cert; reuse it rather than per-service certs.
*Sub-decision:* generate IngressRoutes mechanically from the existing labels rather than
hand-writing 101 objects.

### D4 — Secrets: sops-nix renders k8s Secret manifests, applied by a systemd unit
No Flux yet, so no Flux+SOPS. Proposal: `sops.templates` renders Secret YAML to
`/run/secrets/...` (mode 0400) on the node that applies them.
*Alternative considered:* sealed-secrets (extra controller, extra key to protect);
Flux+SOPS (correct long-term, but blocked on Flux).
*Hard requirement either way:* the plaintext `.env` files must be **deleted** from disk
and a `*.env` rule added to that repo's `.gitignore` (currently absent — `git add -A`
would commit credentials today).

### D5 — Cutover: strictly service-by-service, both stacks coexisting
Docker keeps serving a given service until its k8s replacement is verified. No big bang.
*Consequence:* the `:80`/`:443` conflict must be resolved FIRST (see Phase 1), because
during migration both ingress paths are live simultaneously.

### D6 — Databases migrate by dump/restore, never by copying data directories
3 postgres clusters (`nextcloud-db`, `immich-postgres14`, `infra-postgres-1`) →
StatefulSets on `local-path`. Restore from `pg_dumpall` output, not by moving PGDATA.
*Rationale:* already established on this host — a hot file copy of a live cluster may
simply refuse to start. Nightly dumps exist at `/storage2/backup/dumps/`.
*Note:* the sqlite services (jellyfin, komga, *arr) keep their DBs as hostPath files;
no dump/restore needed, but see R4.

---

## 3. Phases

### Phase 0 — Prerequisites (blocking; do before any migration)
0.1 Resolve the ServiceLB `:80`/`:443` overlap. Decide which ingress owns the ports on
    minas during the transition. Likely: keep docker traefik2 on 80/443, move k3s traefik
    to alternate ports (or keep ServiceLB genuinely off minas) until cutover.
0.2 Decide and document the **control-plane risk** (see R1). This gates whether it is
    acceptable to put 35 production services behind a Pi 5 with sqlite.
0.3 Establish an image path for `pin-collector-model-service:local` — it is a LOCAL BUILD
    with no registry. k3s cannot pull it. Either stand up a registry, import into
    containerd on minas, or build in-cluster.
0.4 Commit the compose fixes made 2026-08-06 (readarr removal, CDI GPU, deluge nft
    entrypoints, nextcloud pin) so the rollback target is a known-good state.
0.5 Take a fresh `backup-root-data` run + ZFS snapshot as the migration rollback point.

### Phase 1 — Cluster foundations
1.1 Namespaces: `media`, `books`, `cloud`, `photos`, `games`, `pincollector`, `infra`.
1.2 Enable CDI in k3s containerd on minas (D2); verify with a throwaway GPU pod.
1.3 Secret pipeline (D4): move all 6 `.env` files into `secrets/minas-tirith.yaml`,
    render as k8s Secrets, verify a pod consumes one, then **delete the plaintext files**.
1.4 Wildcard cert reachable in every namespace (reflector already does this — verify).
1.5 Node labels for data locality: `storage=zfs` on minas; use it in nodeSelectors.

### Phase 2 — Pilot (prove the pattern on cheap services)
Migrate, in order: `flaresolverr` → `wrapperr` → `overseerr`.
Chosen because: stateless, no GPU, no VPN, no /storage mount, low blast radius, and
`overseerr` exercises ingress + a small PVC.
**Exit criterion:** all three served through k3s ingress with valid TLS, docker copies
stopped (not deleted), for 48h with no regression.

### Phase 3 — Bulk migration, by tier
- **T1 stateless + ingress only:** flaresolverr-books, shelfmark, readmeabook, maintainerr, tautulli, tracearr
- **T2 hostPath config + /storage data:** sonarr, radarr, lidarr, animearr, prowlarr, calibre, komga, kavita, audiobookshelf
- **T3 databases (D6):** nextcloud + nextcloud-db + nextcloud-redis; immich + immich-postgres14 + immich-redis; PinCollector postgres/minio/api
- **T4 hard cases:** see Phase 4
Each service: manifest → apply → verify functionally (not just "Running") → stop the
docker copy → record in a migration ledger.

### Phase 4 — Hard cases (do last, individually)
4.1 **GPU: plex, jellyfin.** CDI device request + nodeSelector. Verify `nvidia-smi`
    inside the pod AND an actual hardware transcode, not just device presence.
4.2 **VPN sidecar: deluge-vpn, deluge-books, gluetun.** `network_mode: service:gluetun`
    becomes a multi-container Pod sharing a netns. Carries `privileged: true` and the
    iptables kill-switch — which on this host required repointing to `xtables-nft-multi`
    because NixOS ships **no legacy x_tables modules** (only `x_tables.ko`, for
    `nft_compat`). The k8s equivalent must preserve that, and the kill-switch must be
    verified to actually block traffic when the tunnel drops.
4.3 **palworld** (52 GB saves, hostPath). Single service; low risk but large data.
4.4 **model-service** (Triton, GPU + local build) — blocked on 0.3.

### Phase 5 — Ingress cutover and Docker decommission
5.1 Move `:80`/`:443` on minas to k3s traefik; retire docker `traefik2`.
5.2 Verify all 101 routes resolve with valid TLS.
5.3 Stop and disable the docker daemon on minas; keep compose files + images for
    rollback for at least 30 days.
5.4 Only then remove `virtualisation.docker` from `containers.nix`.

### Phase 6 — Cleanup
6.1 Delete `/storage2/backup-2026-07-30` once every service is verified.
6.2 Fold the migration learnings back into RESTORE-RUNBOOK.md.
6.3 Revisit Flux now that manifests exist.

---

## 4. Risks

**R1 — Control plane is a Pi 5 with sqlite, single server.** Today, Docker on minas
survives pelargir being dead. After migration, existing pods keep running if pelargir
dies (kubelet holds them), but nothing can be changed, restarted, or rescheduled — and
per the owner's own notes, pelargir dying also removes the only path to site A's BMC.
*Mitigations to consider:* make minas a second server (embedded etcd, HA); or accept and
document; or keep the most critical service on Docker. **This is the largest open risk
and it is architectural, not incidental.**

**R2 — Two ingress paths live simultaneously during a service-by-service cutover.**
klipper-lb installs iptables DNAT rather than binding sockets, so it can intercept
traffic ahead of docker-proxy unpredictably. Must be resolved in Phase 0.

**R3 — 101 ingress routes is the bulk of the work** and is the most likely place for a
silent miss (a route that exists but points nowhere). Needs a mechanical diff of
"routes before" vs "routes after", not eyeballing.

**R4 — sqlite databases under hostPath.** jellyfin holds `library.db` with a continuous
write lock (verified: a 60s busy timeout still fails; dumping requires stopping
jellyfin). Moving to k8s does not change this, and the nightly dump story must be
re-established in the new topology.

**R5 — Rollback granularity.** Once a service's data is written by the k8s copy, rolling
back to the docker copy may lose writes. Each migration step needs a defined,
tested rollback, not an assumed one.

**R6 — GPU contention.** plex, jellyfin and model-service all want the RTX 2080. Under
docker they shared it implicitly. Under k8s with CDI there is no scheduler-level
arbitration (that is what the device plugin would give). Decide whether that matters.

---

## 5. What "done" means (acceptance)

1. `kubectl get pods -A` shows all migrated services Running/Ready.
2. All 101 ingress routes answer with valid TLS, verified by an automated sweep that
   diffs against the pre-migration route list.
3. Plex and Jellyfin perform a real hardware transcode.
4. deluge-vpn's kill-switch verifiably blocks traffic when the tunnel drops.
5. Immich, Nextcloud, Plex, Jellyfin each log in and show real pre-existing data.
6. A full backup cycle runs in the new topology, with database dumps present.
7. Docker daemon stopped on minas with zero service impact for 7 days.
8. No plaintext credentials remain on disk.

---

## 6. Questions for the reviewer

Q1. Is R1 (Pi 5 + sqlite control plane for 35 production services) acceptable, or should
    HA / a second server be a prerequisite rather than a follow-up?
Q2. Is D1 (hostPath + nodeSelector) right, or is there a materially better pattern for
    98 TB of existing ZFS data that does not pretend the pods are portable?
Q3. Is D2 (CDI over the nvidia device plugin) right given R6 (three GPU consumers, no
    scheduler arbitration)?
Q4. Is D4 (sops-nix → rendered Secret manifests) sound without Flux, or is that a
    dead-end that should be skipped in favour of going to Flux+SOPS first?
Q5. Is the phase ordering right — specifically, should databases (T3) come before or
    after the GPU/VPN hard cases?
Q6. What is missing entirely? Particularly around rollback, and around anything that
    silently breaks when 35 services stop being on the same docker bridge network
    (service-to-service discovery by container name is used today).
