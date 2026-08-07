# k3s migration — Phase 1: foundations (v2)

**Status:** v3, 2026-08-06.

> **⚠️ REVIEW STATUS.** Codex reviewed v1 (round 3) and v2 (round 5). Both were rejected:
> v2's verdict was *"No. P1A is not executable as written."* **v3 changes the approach
> rather than the prose** — see §0b. A further review is deliberately deferred until
> **P1B (encryption)**, which is the genuinely dangerous window; P1A as redefined below
> is small enough that its blast radius is self-evident rather than argued.

Revised after an adversarial execution review
(`K3S-PHASE1-REVIEW.md`, gpt-5.6-sol, high effort, 1.45M tokens). Verdict on v1:
*"executable only after named changes — do not execute as written."*

**Scope:** everything that must exist before the first minas service migrates. **No
workload moves in Phase 1.** Split into five independently-schedulable change windows
(P1A–P1E) because v1 combined namespace creation, a live manifest refactor, a
control-plane restart, Secret rewriting, certificate changes, a destructive storage test
and GPU runtime work into one undifferentiated phase.

---

## 0. Corrections from review — things v1 stated that were WRONG

Recorded rather than quietly fixed, because several were confidently asserted.

| v1 claim | Reality |
|---|---|
| Enable encryption with `k3s secrets-encrypt reencrypt` | **Wrong procedure for v1.35.6.** The supported existing-cluster flow is `enable` → add `--secrets-encryption` → restart → verify → `rotate-keys` → await `reencrypt_finished` → restart → verify. It is **not** an atomic SQLite rewrite: a controller updates each Secret through the API at roughly 5/sec. |
| Rollback = revert the Nix flag | **There is no transactional rollback.** Removing the flag or key config while encrypted rows remain makes Secrets **unreadable** — breaking mounts, cert-manager, reflector and every restarting home pod. Real rollback is a controlled restore of the pre-change `state.db` + matching server token + server args. |
| Exit criterion: grep the datastore for a Secret value | **Not proof.** Old Kine revisions, SQLite free pages, WAL content and prior restic snapshots retain plaintext. Verify instead that the **latest** record for every `/registry/secrets/` key carries an encryption envelope. Historical exposure cannot be rewritten — it needs **rotation** if unacceptable. |
| Auto-deploy gives a lifecycle | **k3s never deletes resources when a manifest file disappears.** A Nix rollback removes files and leaves the cluster objects. Every change needs an explicit **object-level** rollback list. |
| Reflector is scoped to namespace `home` | It is **annotations on the source Secret** (`ingress.yaml:230`), not a global reflector restriction. |
| "Issue against LE staging first" | Unsafe as written — that would edit the **live production issuer** which already owns certificate reconciliation. Requires a **separate** `letsencrypt-staging` ClusterIssuer. |
| Exclusive GPU allocation is viable | **No.** One physical GPU and two always-running consumers (plex, jellyfin) means one stays **Pending**. Needs the device plugin with **2 time-sliced replicas**. "GPU idle today" is a point-in-time reading, not evidence. |
| `hostPath.type: Directory` gates against an unmounted pool | **It does not.** It proves a directory exists. Docker has a real mount gate (`containers.nix:97`); **k3s has none** (`minas-tirith/k3s.nix`). This is the same empty-mountpoint class that produced the `~`→`/root` and `/etc/timezone` failures on 2026-08-06. |
| P0.7 complete | **Partially.** Taint and architecture audit are done; **container resource requests have no owner** and are absent from every existing manifest. P0.7 is reopened as P1A.5. |

---

## 0b. v2 → v3: approach changed, not wording

Round 5 rejected v2 with *"No. P1A is not executable as written."* The diagnosis was that
the bare namespace operation is safe but **the delivery path was not**. v3 changes the
mechanism rather than defending it:

| Round-5 finding | v3 |
|---|---|
| A.2 changed manifest delivery *before* the "first state change" | A.2 moved **after** A.3 |
| Auto-deploy also creates an AddOn — "one namespace" was false | Canary is **imperative** (`kubectl create ns`); auto-deploy is for permanent objects only |
| Rollback would **recreate** the namespace (YAML stays in auto-deploy) | `kubectl delete ns` — nothing left to reconcile |
| A.6 contradicted A.3 (empty *and* default-deny) | Posture is a separate step, before the first Pod |
| Resource-request owner "does not exist in the ledger" | Named owner + a ledger column |
| Probe semantics **misunderstood** | Corrected: startup *suppresses* other probes, it does not run migrations |
| CoreDNS capacity **not fixed** | Added below |
| PC2 restore drill mandated *and* deferred | Contradiction gone — **the drill passed 2026-08-06** |

**A.7 CoreDNS capacity (round 5: NOT FIXED).** Currently unmeasured, and every migrated
service adds DNS load — the `*arr` mesh alone is chatty. Before Phase 3: record baseline
replica count, placement, resource requests, query latency and SERVFAIL/timeout rates;
set an explicit replica/resource budget for ~32 additional workloads; and test behaviour
during a **pelargir restart**, since CoreDNS runs there and the control plane is single.
"CoreDNS exists" is not a capacity result. Exit criterion: a measured before/after under
load, not a pod count.

> ### ✅ A.7 AVAILABILITY SOLVED — 2026-08-06 (capacity still open)
>
> Baseline measured: **1 replica, on pelargir, 2m CPU** — capacity was never the real
> problem. Availability was. A single replica on the single server meant every DNS
> lookup by every workload on every node depended on pelargir being alive, so a pelargir
> reboot took DNS out fleet-wide. That is a *data-plane* SPOF created by a control-plane
> outage, and it had to be fixed before 35 services arrived.
>
> **Now: 2 replicas, one per node, both Ready, verified answering identically** (both
> resolve `kubernetes.default` → 10.43.0.1 and `home-assistant-external.home` →
> 10.43.136.246), with a PDB permitting either node to be drained but never both.
>
> **The fix deliberately does NOT take ownership of k3s's CoreDNS.** The obvious route —
> `--disable=coredns` plus our own manifest — is a trap, and cross-model review found the
> specific mechanism: `--disable` is an *active teardown* that deletes the AddOn's
> objects including the `kube-dns` Service at 10.43.0.10, and **it matches by manifest
> basename permanently**. A replacement named `coredns.yaml` — the obvious name — is
> deleted on sight forever, i.e. guaranteed fleet-wide DNS loss. Even under a different
> name, the replacement can lose the race with teardown GC, and since auto-deploy is
> checksum-gated nothing recreates the objects until the file changes or k3s restarts.
> "No kube-dns Service indefinitely" is reachable.
>
> Instead `coredns-ha.yaml` adds a second, independently-owned Deployment whose pods
> carry `k8s-app: kube-dns`, so the *existing* packaged Service selects them. k3s keeps
> owning and upgrading its replica; ours is additive, needs no window, and is reversible.
>
> **Known residual, accepted deliberately:** `trafficDistribution: PreferSameNode` cannot
> be set, because k3s owns the Service and reverts the field. So during a pelargir outage
> the EndpointSlice cannot be updated (its controller is on the dead control plane) and
> the dead endpoint persists — roughly half of lookups stall ~5-10s then fail, recovered
> by application retry with probability 1−0.5ⁿ. Materially better than today's 100%
> failure, and the ownership cutover remains available later, rehearsed on a VM first.
>
> ⚠️ **Upgrade coupling:** k3s upgrades the packaged replica and will not upgrade ours, so
> the image pin in `coredns-ha.yaml` must be bumped whenever k3s bumps CoreDNS. Stateless
> resolvers tolerate skew, but it must not drift indefinitely.
>
> **Still open (the original A.7 question):** query latency and SERVFAIL/timeout rates
> under the load of ~32 additional workloads. Redundancy is not a capacity measurement.

---

## 1. Non-negotiable preconditions (before ANY window)

- **PC1 Baseline capture.** Record and store off-host: node list + taints, all AddOns,
  Secret count and names, StorageClasses, every ingress route, workload readiness, the
  **exact four Pending osgiliath pods**, and the 32 running docker containers.
  Any later deviation from this baseline is an abort condition.
- **PC2 Fresh verified datastore checkpoint** — `state.db` + server token **+
  `/etc/rancher/node/password`**, restored into scratch and proven, not merely taken.
  This is the only rollback for P1B.
  ✅ **Satisfied 2026-08-06**: the mechanism is proven end to end — offsite snapshot
  restored in an isolated VM using credentials derived independently of pelargir, with the
  CA fingerprint and a Secret's content matching live. **Take a FRESH checkpoint
  immediately before P1B regardless**; a proven procedure is not a current backup.
- **PC3 Announce the window.** Running pelargir's backup scales **home-assistant,
  zigbee2mqtt and mosquitto to zero** (`pelargir/backup.nix:5`), and minas' backup can
  briefly stop jellyfin. These are user-visible.
- **PC4 Nix revision recorded** for every host, so "what was deployed" is answerable.

---

## 2. P1A — Delivery guardrails (lowest risk, do first)

**A.1** Manifest validation + unique-filename enforcement + an **ownership/prune map**
(which file owns which objects). `manifests.nix:62` copies without validation, atomic set
replacement, or stale-file removal.

**A.2 — AFTER A.3, not before.** Per-host linkFarms in `manifests.nix` (option (b)).
*Organisational separation only — it does NOT reduce runtime blast radius*, since
everything still lands in pelargir's single auto-deploy dir, and a Nix activation
reapplies every manifest. v2 had this ordered before the "first state change", which made
that claim untrue. Do **not** refactor `ingress.yaml` or existing home manifests here.
Requires the ownership/prune map from A.1, because auto-deploy will not remove anything
this stops emitting.

**A.3 FIRST EXECUTABLE STATE CHANGE — `kubectl create namespace migration-canary`.**

⚠️ **Deliberately IMPERATIVE, not through auto-deploy.** v2 put this in the manifest
pipeline and was rejected for it, correctly:

- k3s auto-deploy also creates an **AddOn object** in `kube-system`, so "one namespace and
  nothing else" was false;
- **k3s never deletes resources when a manifest file disappears**, so the stated rollback
  (delete the namespace) would have been undone at the next reconcile — the YAML would
  still be sitting in the auto-deploy directory;
- a Nix activation reapplies **every** manifest, touching live home/ingress/osgiliath
  objects, so the "first" state change was not the first at all.

A canary is temporary and must be cleanly removable, which is exactly what auto-deploy is
bad at. So:

```
create:   k3s kubectl create namespace migration-canary
rollback: k3s kubectl delete namespace migration-canary
```

One object created, one command to remove it, no AddOn, no `manifests.nix` change, no Nix
rebuild, no stale file left behind. Auto-deploy remains the right mechanism for
**permanent** objects — it is adopted in A.2, which now happens *after* this step.

**Honest caveat:** the namespace is not literally empty — Kubernetes always creates a
`default` ServiceAccount (and a `kube-root-ca.crt` ConfigMap) inside it. The check is
therefore *"contains only the objects Kubernetes creates itself"*, not *"contains
nothing"*.

**Verify against PC1** — allowing for normal controller activity rather than aborting on
any diff: 4 home workloads 1/1, svclb 2/2, traefik 1/1, 32 docker containers, and exactly
the 4 pre-existing Pending osgiliath pods.

**A.4 The workload contract** — written once, applied to every future manifest:
- `automountServiceAccountToken: false` unless a named API operation requires otherwise
  (precedent: `osgiliath/manifests/home-assistant.yaml:25`)
- dedicated ServiceAccount only where API access is needed; no RoleBinding without a
  named operation
- **image digest pins**, registry-qualified, `imagePullPolicy: IfNotPresent`, pre-pull
  before cutover, rollback digest recorded (precedent: `pelargir/manifests/home-assistant.yaml:57`)
- probe semantics (**corrected — v2 had this technically wrong**): a **startup** probe
  does not "run migrations"; it *suppresses liveness and readiness checks until the
  container has finished starting*, which is what protects a slow-starting app from being
  killed mid-initialisation. **Readiness** gates traffic and may legitimately fail while a
  dependency is down. **Liveness** is only for unrecoverable self-failure — it must never
  restart an app because its database or DNS is unavailable, which is the classic way a
  dependency outage becomes a cluster-wide restart storm
- `replicas: 1` + `strategy: Recreate` for every single-writer workload
- explicit `terminationGracePeriodSeconds`

**A.5 Resource requests (reopened P0.7).** CPU / memory / ephemeral-storage requests are
mandatory on every canary and every migrated workload, derived from observed docker usage.
**Owner: Edgar, per service, recorded in the ledger before that service migrates.** v2
said "the ledger row owner", which round 5 correctly called out as naming an owner that
does not exist — the ledger has no owner column. Add one, or treat the migrating operator
as owner; either way the requests must exist before the manifest is written. Without requests everything
is BestEffort and the scheduler cannot tell a Pi from a 125 GB host.

**A.6 Namespace posture — applied as its own step, never bundled into A.3.** v2 said the
canary namespace would both be "empty" and "start default-deny", which cannot both be
true. Posture lands as a separate, separately-verified change **before the first Pod**,
not with the namespace. Namespaces get **default-deny NetworkPolicy** (allow DNS,
ingress-controller, monitoring, and the measured dependency edges from the ledger) and
**Pod Security labelled audit/warn first**. Enforcement cannot be blanket: hostPath,
`privileged` and `NET_ADMIN` workloads exist and each needs a named exception owner.

> ### ✅ A.6 ENFORCEMENT PROVEN — 2026-08-06
>
> Writing default-deny policies is worthless if the CNI ignores them, and a policy that
> is silently unenforced is *worse* than none: it reads as protection in the manifest and
> in review while providing nothing. k3s ships flannel, which has **no NetworkPolicy
> support of its own** — enforcement here comes from the separate controller k3s bundles.
> So this was measured before any policy gets written.
>
> Method — the only shape that proves anything: verify traffic **succeeds**, apply
> default-deny, verify the *same* traffic then **fails**. A test that only checks the
> "blocked" half proves nothing, because a broken listener also looks blocked.
>
> | case | before policy | after default-deny | |
> |---|---|---|---|
> | same node (both on minas) | reachable | **blocked** | ✅ |
> | **cross-node** (pelargir → minas, over the tailscale flannel backend) | reachable | **blocked** | ✅ |
>
> The cross-node case is the one that matters and is not implied by the first: that
> traffic traverses the flannel/tailscale datapath, which is a different code path from
> the local bridge. Both are enforced, so default-deny is safe to rely on for the
> migration.
>
> ⚠️ Two false negatives were hit while building this test, both mine, and both would
> have produced a *reassuring* wrong answer — recorded so the trap is not re-entered:
> `nc -q1` is not valid busybox (the listener never started), and `busybox httpd -h /tmp`
> serves `index.html`, so a file named anything else returns 404. In both cases the
> "after" check duly reported blocked. **Had the baseline step been omitted, this
> document would now claim enforcement was verified when nothing had been tested at all.**

## 3. P1B — Encryption at rest (own window, control-plane restart)

Currently `Disabled, no configuration file found`. Do this **before** creating any new
application Secret — not because it is harder afterwards, but to avoid a plaintext
exposure window and another backup generation containing cleartext.

> ### Cross-model audit of this procedure — 2026-08-06
>
> **The step order below is CORRECT for v1.35.6.** The specific worry that it might be
> backwards — that `secrets-encrypt enable` needs the server already started with
> `--secrets-encryption` — applies only to k3s **before v1.30**, where `enable` failed
> without an existing encryption-config file. In v1.35.6 `enable` writes the config and
> saves bootstrap state, and the reverse order converges harmlessly anyway.
>
> Also confirmed: the flag still exists and **is required alongside** the CLI command
> (they cooperate rather than conflict); **kine/sqlite fully supports `rotate-keys`**
> despite most k3s encryption docs assuming embedded etcd; reencryption is API-driven at
> roughly 5 Secrets/sec, so 22 Secrets completes in about 5 seconds; and existing
> Secrets are **not** re-encrypted automatically — `rotate-keys` is what rewrites them.
>
> **The backup question, which mattered most:** `state.db` + token backups **remain
> restorable** after encryption is enabled, because k3s embeds the EncryptionConfig and
> its hash in the **token-encrypted bootstrap record inside the datastore** — the same
> mechanism the PC2 drill already proved for the CAs. Conditions: take the backup
> *after* `rotate-keys`, rehydrate into an **empty** datadir on restore, and **re-run
> the restore drill against an encrypted-era snapshot** before the 35 services migrate.
> Until that re-drill passes, P1B is not complete.
>
> ⚠️ **NixOS-specific hazard, and it is the dangerous one.** A generation rollback
> (`nixos-rebuild --rollback`, or picking an older generation) **silently removes
> `--secrets-encryption`** while encrypted rows remain in the datastore, producing a
> cluster-wide Secret read outage that is both silent and delayed — running pods keep
> their mounted Secrets and only fail on restart, so the rollback appears to have
> worked. `pelargir/ROLLBACK.md` documents `switch --rollback` as the *primary*
> recovery, so that file was given a blocking warning at its top **before** this
> window opens.
>
> Two cosmetic items remain unverified (the exact status string after `enable`, and
> whether status reads Disabled or Enabled between the flag restart and rotation)
> because the auditing sandbox could not reach k3s docs. Neither changes the procedure.

**Procedure (v1.35.3+ — do not use the legacy single command):**
1. PC2 checkpoint verified; record Nix revision and server args.
2. `k3s secrets-encrypt enable`
3. Add `--secrets-encryption` to the server config; restart.
4. Verify status/stage; confirm hashes match.
5. `k3s secrets-encrypt rotate-keys`
6. Await `reencrypt_finished`; restart; verify `Enabled` and matching hashes.

**If interrupted:** the datastore may hold a mixture of plaintext / old-key / new-key
objects. That mixture stays readable **only while the config retains the identity and
prior-key providers**. Correct response is **roll forward** — preserve config, resume.
Do **not** remove the flag.

**Rollback:** stop k3s, restore the exact pre-change datastore + token + args. A
supported *disable* must first rewrite all Secrets to plaintext.

> ### ✅ P1B EXECUTED — 2026-08-06
>
> | step | result |
> |---|---|
> | fresh PC2 checkpoint | 1651 kine rows, token + node-password + full server args |
> | `secrets-encrypt enable` | `secrets-encryption enabled`, identity provider written |
> | `--secrets-encryption` + restart | apiserver running with `--encryption-provider-config` |
> | verify | `Disabled` / stage `start` / **all hashes match** |
> | `rotate-keys` | `keys rotated, reencryption finished` (~5 s for 22 Secrets) |
> | restart + verify | **`Enabled`**, stage `reencrypt_finished`, hashes match, key `aescbckey-2026-08-06T19:44:59-07:00` |
>
> **Proven against the raw datastore, because `status: Enabled` is self-reported:**
> at the latest live revision of every Secret — **22 encrypted, 0 plaintext**, envelope
> `k8s:enc:aescbc:v1:aescbckey-…`. Plus a canary: a Secret created with a known literal
> appears in **0** datastore rows while reading back correctly through the API.
>
> Throughout, the PC1 baseline held exactly — 22 Secrets readable, both nodes Ready,
> and exactly 4 non-running pods (the expected Pending osgiliath set). No deviation.
>
> **Both `[RECHECK]` items from the audit are now resolved empirically:**
> - After `enable`, `status` **fails** with `secret-encrypt error ID …: missing
>   annotation on node pelargir`. That is the staged state, not a fault — the annotation
>   is written when the server restarts with the flag. It also recurs *transiently* right
>   after that restart: the status call at 19:44:14 failed and the annotation landed at
>   19:44:15. **Retry before diagnosing.**
> - Between the flag restart and rotation, status reads **`Disabled`, stage `start`** —
>   correct, because the provider is still `identity` (passthrough). Encryption only
>   becomes active when `rotate-keys` installs the AES key.
>
> **Checkpoints:** the pre-encryption datastore + token is preserved at
> `/var/lib/k3s-preP1B` as the rollback target, and a post-`rotate-keys` checkpoint
> (verified 22/22 encrypted) is staged. The offsite copy arrives with the 03:30 restic
> run; it was **not** forced early because that run scales Home Assistant, Zigbee2MQTT
> and Mosquitto to zero (PC3), and the pre-encryption snapshot already offsite remains a
> valid recovery target in the meantime.
>
> ### ✅ ENCRYPTED-ERA RESTORE DRILL PASSED — P1B COMPLETE
> Re-run against an encrypted-era snapshot in an isolated VM (`pelargir/restore-drill-vm.nix`,
> now committed so it is repeatable). Recovered 7 namespaces, 22 Secrets, 18 ConfigMaps,
> 17 Deployments.
>
> | assertion | result |
> |---|---|
> | `encryption-config.json` regenerated — it is **not** in the backup | **PRESENT**, providers `aescbc identity` |
> | `secrets-encrypt status` in the restored cluster | **Enabled** |
> | rows ciphertext on disk | **22 / 22** |
> | CA fingerprint vs live | **EQUAL** — `6C:02:09:C4:0C:A2:CB:3F:49:34:71` |
> | 3 Secrets' content hashes vs live | **EQUAL** |
>
> The CA equality matters as much as the Secrets: without it a freshly-initialised
> cluster could masquerade as a successful restore. And matching *content* hashes are
> what prove decryption actually happened — listing Secrets proves nothing.
>
> **This settles the audit's most important open question:** the backup holds
> `state.db` + token and **not** the encryption config, and k3s does rehydrate that
> config from the token-encrypted bootstrap record. Post-P1B backups are restorable.
>
> ⚠️ **The first run FAILED, and the failure was worth having.** It hardcoded the
> pre-P1B server arguments and so omitted `--secrets-encryption`: k3s started with the
> identity transformer, could not read one Secret (`identity transformer tried to read
> encrypted data`), and the API never went ready. That is the ROLLBACK.md failure mode
> reproduced on real data — and to anyone who had not just written that warning it
> would have been indistinguishable from a corrupt backup. The drill now derives its
> server args from `PROVENANCE.txt` instead of hardcoding them, so it cannot drift from
> the cluster it certifies.

**Note:** existing home / cert-manager / controller Secrets require migration regardless.
Historical plaintext in old restic generations cannot be rewritten — rotate if that
matters.

## 4. P1C — Secret pipeline + ingress canary

**C.1 New `secrets/cluster-apps.yaml`, recipients admin + pelargir.** Do **not** add
pelargir to `secrets/minas-tirith.yaml` — that would hand it minas' console password and
k3s token for no reason and undo `c8870ee`'s isolation.

**C.2 Runtime-only rendering.** Render to `/run`, apply from there via a retrying oneshot
gated on API readiness. Build values with `kubectl create secret --dry-run=client -o json`
or equivalent — **not** by interpolating `.env` values into quoted YAML (PIA passwords and
JWT secrets contain YAML-hostile characters). Define rotation, deletion, stale-apply
alerting and consumer-restart semantics.

**C.3 Migrate the EXISTING home-secret pipeline too.** `manifests.nix:67` copies rendered
plaintext into persistent storage today. Fixing only new Secrets leaves the exit criterion
false.

**C.4 Separate `letsencrypt-staging` ClusterIssuer** + a unique canary hostname. Never
touch the production issuer (`ingress.yaml:201`) or the existing pelargir certificate.

**C.5 Certificates: one per namespace, issued into that namespace.** Not one 30-SAN key
reflected everywhere — that makes one private key's blast radius every service. Generate
each namespace's SAN list from **active, non-parked** routes immediately before issuance;
the ledger's ~30 names are an inventory snapshot, not an issuance set.

## 5. P1D — Storage foundation

**D.1 ZFS readiness gate for k3s (blocking).** Keep a storage taint on minas until a host
service proves both pools **and** the required datasets are mounted. `hostPath.type:
Directory` is not a mount-identity check.

**D.2 Static local PVs for databases** — explicit ZFS dataset paths, `Retain`, PV node
affinity to minas, pre-bound PVCs, measured capacity + growth headroom, backup inclusion,
and a **rebind/restore drill**. `local-path` + `Retain` alone is insufficient: PVC sizes
are requests not quotas, `WaitForFirstConsumer` lets the first schedule silently decide
which node owns data forever, and `Retain` preserves a released directory without making
reattachment easy or safe.

**D.3 Destructive test on a dedicated dataset only** — uniquely named class/path, confirm
PV is `Retain` before deleting the PVC, prove rebind and recovery, then remove objects in
PVC → PV → path order.

## 6. P1E — GPU foundation (need only precede plex/jellyfin migration)

NVIDIA device plugin restricted to minas, with **2 time-sliced replicas**. Document that
time-slicing gives scheduling accounting but **not** memory or fault isolation. Verify a
real CUDA workload and **concurrent transcodes**, plus failure of one consumer — not
device presence. CDI is proven under **docker only**; the k3s containerd path is untested.

⚠️ The containerd/toolkit changes this needs can disturb **docker's** GPU access, which
live plex and jellyfin depend on. Rollback: remove the DaemonSet/config, restore runtime
config, and prove docker `nvidia-smi` plus real transcodes.

---

## 7. Blast radius and rollback

| Step | How it breaks the live estate | Rollback |
|---|---|---|
| A.3 namespace | Additive; risk only if labels/admission are bundled in | Delete the **empty** namespace after confirming it owns nothing |
| A.2 manifest refactor | Reapplies home/ingress/osgiliath objects through the sole server; omissions survive Nix rollback | Restore files **and** explicitly restore/delete each object; verify HA, svclb, traefik |
| P1B encryption | Restarts the only server; rewrites every Secret; bad state breaks Secret reads cluster-wide | Roll **forward** with config retained; else restore the PC2 checkpoint exactly |
| C.2 secrets | Can overwrite live home/cert-manager Secrets; env consumers do not reload | Reapply previous payload from sops, restart identified consumers only; never auto-prune |
| C.4/C.5 certs | Editing live issuer/cert disrupts HA TLS; a reflected key can overwrite same-named Secrets | Touch nothing existing; delete only new staging objects; keep the serving TLS Secret until the new one is Ready |
| P1D storage | A wrong default-class annotation captures new claims; the deletion test is destructive; I/O affects plex/immich/nextcloud | Unique test class/path/dataset; verify `Retain` before deleting |
| P1E GPU | Runtime changes can break docker GPU for live plex/jellyfin | Remove plugin, restore runtime config, prove docker transcodes |
| Backup/monitor proof | **Scales HA, Z2M, Mosquitto to zero**; minas backup can stop jellyfin | Announce first; verify scale-up traps; manually restore if cleanup fails |

**Osgiliath's 4 Pending pods are inert** — distinct namespace, hostPaths not PVCs — so
namespaces and StorageClasses will not disturb them. Their eventual auto-start when
osgiliath joins is an **independent event to fence and observe**; any earlier transition
is an abort condition.

---

## 8. Exit criteria (each observable, in order)

1. `migration-canary` namespace exists; PC1 baseline otherwise unchanged.
2. `k3s secrets-encrypt status` = Enabled **and** the latest record for every
   `/registry/secrets/` key carries an encryption envelope. (Raw grep is **not** proof.)
3. A canary Secret is rendered by sops-nix, applied from `/run`, consumed by a pod, and
   **no plaintext copy exists on persistent storage** — including the migrated home pipeline.
4. A canary route serves with a **staging** cert through a separate issuer, with the
   production issuer and pelargir's certificate untouched.
5. A canary Deployment on minas carries requests, digest-pinned image, correct probe
   classes, `automountServiceAccountToken: false`, and a default-deny NetworkPolicy — and
   still works.
6. A static local PV on a dedicated dataset survives PVC deletion; data rebinds and restores.
7. GPU canary runs a real CUDA workload **and** two concurrent transcodes; docker
   `nvidia-smi` still works for live plex/jellyfin.
8. Backup and monitoring detect a **deliberately failed** canary — not merely count it.
9. Pelargir unchanged throughout: 4 home workloads 1/1, svclb 2/2, traefik 1/1, ingress serving.

## 9. Deferred out of Phase 1

Production certificates for parked services · PinCollector and its two `:local` images
(P0.9) · Nextcloud 28→34 · the `wolf` gameserver's `runtime: nvidia` · the **restore
drill**, still the largest open gap and still needing scratch hardware · Flux (Phase 6).
