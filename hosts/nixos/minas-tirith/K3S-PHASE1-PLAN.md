# k3s migration — Phase 1: foundations

**Status:** DRAFT for review, 2026-08-06. Follows `K3S-MIGRATION-PLAN.md` (v3) and its
two cross-model reviews (`K3S-MIGRATION-REVIEW.md`).

**Scope:** everything that must exist in the cluster *before* the first minas service
migrates. No workload moves in Phase 1. Exit criteria in §7.

---

## 0. Where Phase 0 actually landed

| | Item | State |
|---|---|---|
| P0.3 | Datastore backup | ✅ k3s sqlite datastore + server token in every restic snapshot |
| P0.4 | Offsite backup | ✅ pelargir → minas working; preflight exits 0 (was skipping silently) |
| P0.5 | Dependency matrix | ✅ captured; mostly hidden in app SQLite, not env |
| P0.6 | Runtime-agnostic backup/monitoring | ✅ both now see docker AND k3s |
| P0.7 | Scheduling isolation | ✅ pelargir tainted `node-role.kubernetes.io/control-plane:NoSchedule` |
| P0.8 | `:80`/`:443` overlap | ✅ resolved — svclb runs only on pelargir; minas has 0 k8s containers |
| P0.10 | 35-row ledger | ✅ `K3S-MIGRATION-LEDGER.md` |
| P0.9 / P0.11 | Registry / compose commit | deferred / dropped (owner) |
| — | **Restore drill** | ❌ **STILL OPEN** — the one genuine gap; needs scratch hardware |

**Measured facts Phase 1 depends on** (not assumptions):
- Cluster: pelargir = the only server (Pi 5, sqlite, now tainted); minas = agent,
  **zero** k8s workloads; osgiliath configured but **not deployed**, its 4 pods Pending 18h+.
- **All manifests are delivered from pelargir's auto-deploy dir**
  (`/var/lib/rancher/k3s/server/manifests`), built by `hosts/nixos/pelargir/manifests.nix`
  via `pkgs.linkFarm` — *including osgiliath's* (`../osgiliath/manifests/...`).
  There is no per-node manifest delivery, and an agent has no auto-deploy dir.
- **Secrets encryption at rest is `Disabled, no configuration file found`.**
- **Only one StorageClass: `local-path` (default), `WaitForFirstConsumer`, reclaim `Delete`.**
- sops is strictly per-host: `secrets/minas-tirith.yaml` is decryptable by **minas + admin
  only**; pelargir is *not* a recipient (`.sops.yaml:48-56`).
- GPU consumers are **2** (plex, jellyfin). GPU idle: 1 MiB / 8192 MiB, 0%.
- Only **immich** is single-arch amd64; everything else publishes arm64 too.
- 16 services are pinned to minas by `/storage*` bind mounts.

---

## 1. P1.1 — Namespaces

`media`, `books`, `cloud`, `photos`, `games`. **Not** `infra` — PinCollector is parked
(see plan §0b), so creating its namespace now would be scaffolding for nothing.

Delivered as a manifest through pelargir, matching the existing `namespace.yaml` /
`osgiliath-namespace.yaml` pattern.

## 2. P1.2 — Manifest delivery model ⚠️ decide explicitly

Everything today goes through pelargir's auto-deploy dir. Continuing that for ~35 minas
services means pelargir's `manifests.nix` linkFarm grows to carry another host's entire
workload set, and **any manifest error takes effect cluster-wide via a Pi**.

Options:
- **(a) Extend the existing linkFarm.** Consistent with osgiliath; zero new machinery;
  but one file list for three hosts and no per-host blast radius.
- **(b) Separate linkFarm per host, still applied from pelargir.** Same delivery, clearer
  ownership, small refactor of `manifests.nix`.
- **(c) Stop using auto-deploy; apply from a systemd unit with an explicit kubeconfig.**
  Gives prune/drift control that auto-deploy lacks — k3s auto-deploy **never deletes**
  what it applied unless the file is removed, and has no drift detection. Round 2 called
  unmanaged `kubectl apply` "not a lifecycle"; auto-deploy is only marginally better.

**Proposal: (b) now, (c) revisited with Flux in Phase 6.** Recorded because Phase 3 will
add ~35 manifests and the wrong choice is expensive to unwind.

## 3. P1.3 — Secret pipeline ⚠️ the real blocker

**The gap round 2 identified is structural, not cosmetic.** Manifests are applied by
**pelargir**, but minas' application secrets live in `secrets/minas-tirith.yaml`, which
**pelargir cannot decrypt**. So there is no host today that can both render those Secrets
and apply them.

Options:
- **(a) New `secrets/cluster-apps.yaml`, recipients = admin + pelargir.** Cluster-scoped
  app secrets, separate from host secrets. Keeps per-host isolation intact for host
  secrets (passwords, tokens); the new file holds only things that become k8s Secrets.
- **(b) Add pelargir as a recipient of `secrets/minas-tirith.yaml`.** Fewer files, but
  deliberately breaks the per-host isolation `c8870ee` established, and hands pelargir
  minas' console password and k3s token for no reason.
- **(c) Give minas a scoped deploy credential and apply from minas.** Minas is an agent
  with no kubeconfig; this invents a new credential and a new failure mode.

**Proposal: (a).** Least privilege, no new credential, no isolation loss.

Mechanics follow the working `pelargir-home-secrets.yaml` pattern
(`hosts/nixos/pelargir/secrets.nix:51`), with two corrections round 2 demanded:
- render to `/run` and apply from there — **do not** copy plaintext manifests to
  persistent storage, which the current pattern does;
- generate values as JSON/base64 rather than interpolating arbitrary `.env` values into
  quoted YAML (PIA passwords and JWT secrets will contain YAML-hostile characters).

**Also P1.3: enable encryption at rest.** It is currently `Disabled`. Without it,
acceptance criterion 8 ("no plaintext credentials on disk") is **false regardless of
`.env` deletion**, because Secret values sit in the datastore in cleartext — and that
datastore is now in every restic snapshot, so the exposure travels offsite.

Sequencing note: enabling encryption does **not** rewrite existing Secrets. Existing ones
must be re-written (`k3s secrets-encrypt reencrypt`) or they stay plaintext on disk.

## 4. P1.4 — Certificate and reflector

Downgraded from "redesign" once measured (plan D3): the `letsencrypt` ClusterIssuer is
**Ready** and its solver selector is `dnsZones: [saldivar.io]` — the whole zone — and two
certs already issue through it, proving the Cloudflare token's scope.

Work:
1. A Certificate covering the ~30 real hostnames from the ledger. `*.saldivar.io` does
   **not** cover `admin.pin.saldivar.io` (two labels) — but PinCollector is parked, so
   that SAN is deferred with it.
2. Widen reflector's `reflection-allowed-namespaces` / `reflection-auto-namespaces` from
   `home` to the new namespaces.
3. **Issue against LE staging first** (INSTALL-RUNBOOK's own guidance) — production certs
   for hostnames nothing serves yet burn rate limit.

Open: one cert with ~30 SANs vs per-namespace certs. One cert = one private key reflected
into five namespaces, so its blast radius is every service. Per-namespace = more objects,
smaller radius.

## 5. P1.5 — GPU device plugin

D2 reversed CDI → NVIDIA device plugin so the scheduler can see the resource. Deploy the
plugin **restricted to minas** (`nodeSelector: kubernetes.io/hostname=minas-tirith`) —
pelargir is ARM with no NVIDIA hardware, and osgiliath does not exist yet.

Then verify with a throwaway GPU pod on minas: `nvidia-smi` inside the pod **and** a real
workload, not device presence. CDI is proven working under docker
(`nvidia.com/gpu=all` verified) but that does **not** prove the k3s containerd path.

Contention is smaller than the plan assumed — only plex and jellyfin, GPU idle — so
exclusive allocation is viable and time-slicing may be unnecessary. Decide explicitly.

## 6. P1.6 — StorageClass for stateful workloads ⚠️ new, not in v3

Only `local-path` exists and its reclaim policy is **`Delete`**: deleting a PVC destroys
the data. That is unacceptable for the three PostgreSQL clusters and every stateful
service, and round 2 flagged it as missing.

Work: add a second StorageClass (`local-path-retain`) with `reclaimPolicy: Retain`, and
require it for all stateful workloads. Also pin those PVCs to minas — `local-path` +
`WaitForFirstConsumer` binds to wherever the pod first schedules, so without a
nodeSelector the *first* schedule silently decides which node owns the data forever.

---

## 7. Exit criteria

1. Five namespaces exist.
2. A canary Deployment with a hostPath volume and a nodeSelector runs on minas and is
   reachable through k3s ingress with a **staging** cert.
3. A canary Secret is rendered by sops-nix, applied from `/run`, consumed by a pod, and
   **no plaintext copy exists on persistent storage**.
4. `k3s secrets-encrypt status` reports Enabled, and existing Secrets have been
   re-encrypted — verified by grepping the raw datastore for a known Secret value and
   **not** finding it.
5. A GPU canary pod on minas runs a real CUDA workload; `kubectl describe node` shows the
   GPU as an allocatable resource.
6. A PVC on `local-path-retain` survives PVC deletion with its data intact.
7. Backup and monitoring both see the canaries (P0.6 already made them runtime-agnostic —
   this proves it against real k8s workloads rather than an empty cluster).
8. Nothing on pelargir regressed: 4 home workloads 1/1, svclb 2/2, traefik 1/1, ingress
   serving.

---

## 8. Questions for review

Q1. §2 manifest delivery — (a), (b) or (c)? Is growing a Pi-hosted linkFarm to ~35
    additional manifests a real risk or an imagined one?
Q2. §3 secret pipeline — is a new `cluster-apps.yaml` (recipients admin+pelargir) right,
    or is there a better answer that does not weaken per-host isolation?
Q3. Encryption at rest — any reason NOT to enable it now, given the datastore now travels
    offsite in restic snapshots? What breaks during `reencrypt` on a single-server cluster?
Q4. §4 — one ~30-SAN certificate reflected into five namespaces, or per-namespace certs?
Q5. §5 — with only two GPU consumers and an idle GPU, is the device plugin still the right
    call over CDI, or was D2's reversal over-cautious now that contention is measured?
Q6. §6 — is `Retain` + nodeSelector sufficient for the databases, or do they need
    statically provisioned PVs with explicit node affinity instead of local-path?
Q7. What is missing entirely — especially anything that only shows up once real workloads
    exist, and anything about ORDER within Phase 1 (what must precede what).
Q8. Is Phase 1 too large? Should the GPU plugin or the StorageClass work be split out so
    the first canary lands sooner?
