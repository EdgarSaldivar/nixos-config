# Codex review — K3S-PHASE1-PLAN.md v2

gpt-5.6-sol · effort high · 2026-08-06

## Verdict

**No. P1A is not executable as written.** The bare Kubernetes operation—creating one namespace—is low risk. The proposed delivery path is not: A.2 changes manifest delivery before A.3, the activation script rewrites every existing manifest, auto-deploy creates an AddOn as well as the namespace, and the stated rollback will recreate the namespace.

P1B is also not yet executable. Its encryption sequence is substantially closer to k3s v1.35.6, but the exact expected intermediate states, failure handling, datastore verification, and atomic restore procedure remain unspecified.

The parent v4 reversal to a recoverable single server does not invalidate Phase 1; v2 consistently treats pelargir as the sole server. However, the parent still calls a restore drill blocking while v2 both requires one in PC2 and defers it in §9. That contradiction directly affects P1A because PC2 is declared mandatory “before ANY window.” See [K3S-MIGRATION-PLAN.md](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-ae6qh3z9/run-1786062030-98887/hosts/nixos/minas-tirith/K3S-MIGRATION-PLAN.md:298>) and [K3S-PHASE1-PLAN.md](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-ae6qh3z9/run-1786062030-98887/hosts/nixos/minas-tirith/K3S-PHASE1-PLAN.md:39>).

## Round-3 findings: fixed or not (one line each, then detail)

- **Encryption procedure — PARTIALLY FIXED.** It now uses `enable` and `rotate-keys`, but omits exact intermediate assertions and a failure branch.
- **Encryption rollback — PARTIALLY FIXED.** It correctly rejects “remove the flag,” but “restore datastore + token + args” is not an executable restore procedure.
- **Datastore-grep exit criterion — PARTIALLY FIXED.** It rejects raw grep, but supplies no correct query and mishandles deleted-key tombstones.
- **Auto-deploy pruning — PARTIALLY FIXED.** The limitation is acknowledged; stale-file and AddOn/object deletion are still not implemented.
- **Reflector scoping — FIXED.** v2 correctly identifies source-Secret annotations instead of a global namespace restriction.
- **Staging issuer — PARTIALLY FIXED.** A separate issuer is correct, but its unique ACME account-key Secret and exact validation are unspecified.
- **GPU time-slicing — FIXED.** Two advertised replicas for the two live consumers is the correct scheduling policy, with isolation limitations stated.
- **hostPath versus ZFS mount identity — PARTIALLY FIXED.** A taint-backed mount gate is the right direction, but no exact identity test or failure behavior exists.
- **Static PVs versus local-path+Retain — FIXED for databases.** The procedure now chooses static local PVs, node affinity, Retain, prebinding, and rebind testing; policy for other new state remains unstated.
- **Resource-request ownership — NOT FIXED.** “Ledger row owner” names an owner that does not exist in the ledger.
- **RBAC / NetworkPolicy / Pod Security — PARTIALLY FIXED.** Useful principles were added, but no concrete policy or deployer RBAC exists, and PSA exceptions are misunderstood.
- **Image digests — FIXED.** Registry-qualified digest pins, pre-pull, and rollback digests are now explicit.
- **Probe semantics — MISUNDERSTOOD.** The liveness warning is good, but “startup for init/migration” is technically wrong.
- **CoreDNS capacity — NOT FIXED.** There is no capacity measurement, failure test, replica/resource budget, or exit criterion.
- **P1A–P1E split — PARTIALLY FIXED.** The five windows improve isolation, but A.2 precedes the alleged first state change and P1C still bundles several live-risk changes.

Detail:

The encryption changes at [§3](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-ae6qh3z9/run-1786062030-98887/hosts/nixos/minas-tirith/K3S-PHASE1-PLAN.md:92>) are substantively better, not merely rewritten prose. The remaining defects are described below.

The replacement datastore criterion at lines 195–196 is not executable. It must define a SQLite/Kine query selecting the newest **live** revision per currently existing Secret, excluding deletion tombstones, then assert the expected `k8s:enc:…` envelope. “Every `/registry/secrets/` key” includes historical/deleted records whose newest value may be a tombstone. The result must also be reconciled against `kubectl get secrets -A`, otherwise omitted keys can make the check pass.

The pruning correction is incomplete. A.1 proposes an “ownership/prune map,” but the actual activation code only installs files and never removes stale destinations ([manifests.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-ae6qh3z9/run-1786062030-98887/hosts/nixos/pelargir/manifests.nix:62>)). K3s explicitly documents that removing a manifest file does not delete its resources. An object map is useful documentation, not pruning. See [K3s auto-deploy documentation](https://docs.k3s.io/installation/packaged-components).

Reflector is genuinely corrected: the namespace lists are annotations on the generated source Secret, visible in [ingress.yaml](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-ae6qh3z9/run-1786062030-98887/hosts/nixos/pelargir/manifests/ingress.yaml:230>). Per-namespace Certificates also remove the need to reflect the new private keys.

C.4’s separate staging issuer is the right design, but must require its own `privateKeySecretRef`. Reusing production’s `letsencrypt-account-key` with the staging directory is not a safe implementation. Its exit test should inspect `Certificate.spec.issuerRef`, the staging issuer’s ACME URL, the resulting chain, and a snapshot proving production specs and Secrets were unchanged.

D.1 changes the storage design appropriately, but “proves both pools and required datasets are mounted” is still prose. Define expected ZFS dataset GUID/name → mountpoint mappings, `findmnt`/ZFS checks, the taint key/effect, who removes it, and what happens if identity is later lost. `NoSchedule` only blocks new Pods; it does not stop an already-running writer after an unmount.

A.5 does not assign ownership. The ledger has no Owner column ([K3S-MIGRATION-LEDGER.md](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-ae6qh3z9/run-1786062030-98887/hosts/nixos/minas-tirith/K3S-MIGRATION-LEDGER.md:15>)). Also, v2’s claim that requests are absent from every existing manifest is false: existing pelargir and osgiliath manifests already contain requests, for example [home-assistant.yaml](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-ae6qh3z9/run-1786062030-98887/hosts/nixos/pelargir/manifests/home-assistant.yaml:9>).

A.6’s “named exception owner” is not how namespace-level Pod Security Admission works. A namespace labelled privileged enough for `hostPath`/`privileged` workloads weakens that whole namespace; exceptions are not attached per Deployment. The plan must either split privileged workloads into dedicated namespaces or explicitly accept namespace-wide posture. Audit/warn labels also need named levels and pinned versions. NetworkPolicy needs actual ingress and egress tests, not merely policy presence.

The probe contract should say: init containers or explicit Jobs perform initialization/migration; startup probes protect slow-starting application containers from readiness/liveness evaluation; readiness controls endpoint eligibility; liveness tests only local unrecoverable failure. Exact endpoints, periods, thresholds, and dependency failure tests remain per-workload deliverables.

CoreDNS is simply absent. “Allow DNS” in NetworkPolicy and “do not use DNS failure as liveness” do not establish DNS capacity or resilience.

## The encryption procedure, step by step

1. **Checkpoint:** Correct requirement, but internally contradicted. PC2 requires a fresh checkpoint restored into scratch; §9 defers “the restore drill.” The measured online SQLite backup is technically sound—[backup.nix](</private/var/folders/cp/vs972dd56496klwfx_pgwkv40000gn/T/codex-seat-wt-ae6qh3z9/run-1786062030-98887/hosts/nixos/pelargir/backup.nix:31>) correctly uses SQLite `.backup` and captures the server token—but backup validation is not a k3s restore proof.

2. **`k3s secrets-encrypt enable`:** Correct for v1.35.6. It creates an identity-only encryption configuration and saves cluster bootstrap state; it does not yet encrypt Secrets. Before it, assert the exact version and `Disabled, no configuration file found`; capture the command exit code and server logs.

3. **Add `--secrets-encryption`, restart the sole server:** Correct and necessarily causes API downtime. The Nix generation and effective server arguments must be captured, not merely the intended source setting.

4. **First status check:** Incomplete. The required result is exactly `Encryption Status: Disabled`, `Current Rotation Stage: start`, and matching hashes. “Verify status/stage” does not say which status or stage is safe to continue from.

5. **`k3s secrets-encrypt rotate-keys`:** Correct command. In v1.35.6 it adds/activates a key, reloads the encryption configuration, updates live Secrets through the API, removes obsolete keys only after success, and saves bootstrap state. The command is synchronous on success; it prints that reencryption finished. If it exits nonzero or times out, do not restart blindly—inspect status, server logs, and `SecretsUpdateError` events while preserving the key configuration.

6. **Await, restart, final status:** Waiting for `reencrypt_finished` before restart is redundant after a successful synchronous command but safe. The official existing-cluster flow is rotate, restart with the same arguments, then verify `Enabled`, `reencrypt_finished`, and matching hashes. See the [official k3s procedure](https://docs.k3s.io/cli/secrets-encrypt) and [v1.35.6 handler](https://github.com/k3s-io/k3s/blob/v1.35.6%2Bk3s1/pkg/server/handlers/secrets-encrypt.go#L354-L444).

7. **Post-verification:** Missing. Read every current Secret through the API; verify every Secret-backed live workload still mounts/starts; create/read/delete a canary Secret; and run the correctly scoped latest-live-record envelope query.

8. **Interruption handling:** The roll-forward preference and “do not remove the flag” warning are correct. “Resume” must name the permitted states and command for each; operators must not improvise from `reencrypt_active`, a timeout, or a hash mismatch.

9. **Rollback:** Conceptually correct but operationally insufficient. An executable rollback must specify: stop k3s; restore the exact k3s version and pre-change Nix generation/arguments; atomically replace `state.db`; remove or replace post-change `state.db-wal` and `state.db-shm`; restore the matching server token with ownership/mode; ensure the configured token source matches; start k3s; assert encryption is again disabled; and verify Secrets and all baseline workloads. Restoring the checkpoint also discards every unrelated cluster mutation after it, so the allowable rollback interval must be bounded.

Finally, “historical plaintext … rotate if that matters” must say **rotate and revoke the application credentials**, not rotate the datastore encryption key. Encryption-key rotation cannot remove plaintext from retained restic generations.

## Is P1A.3 safe as the first step

**The Namespace API operation is safe; P1A.3 as delivered is not genuinely “one empty namespace and nothing else.”**

- A.2 appears before A.3 and is itself state-changing. The plan admits that it reapplies home, ingress, and osgiliath objects through the sole server (lines 176–177).
- A Nix activation runs the existing install loop for every manifest. Even unchanged files may be touched and observed by auto-deploy, reapplying live objects.
- Auto-deploy creates an `AddOn` in `kube-system`; therefore the namespace is not the only cluster object added.
- Kubernetes automatically creates a `default` ServiceAccount in every namespace, so “empty” and “confirming it owns nothing” are false literally. See [Kubernetes Service Accounts](https://kubernetes.io/docs/concepts/security/service-accounts/).
- A.6 contradicts A.3. If default-deny NetworkPolicy is bundled, it is not an empty namespace; if it is not bundled, the namespace does not “start default-deny.” Apply posture as a separately verified step before the first Pod.
- A malformed or multi-document canary file could create more than a Namespace unless validation explicitly asserts one document, kind/name, and no unexpected metadata/admission labels.
- PC1’s “any deviation” rule can abort on normal controller activity. It needs a semantic allowlist: the new Namespace, AddOn, controller-created namespace objects, and expected Events, while comparing live workload readiness and immutable specs separately.

The rollback “delete the empty namespace” is insufficient. If the YAML remains in the auto-deploy directory, it can be reapplied. A Nix rollback still does not remove the stale destination file, and merely deleting the file does not delete the AddOn’s resources. The rollback must remove the declarative source, remove the stale on-disk file, delete/disable the AddOn with its ownership semantics understood, delete the namespace, and prove none of those objects reappear.

## Exit criteria: observable or prose

1. **Partly observable.** Namespace existence and named readiness counts are mechanical; “baseline otherwise unchanged” needs a normalized baseline schema and allowed-difference list.
2. **Not mechanically checkable as written.** No Kine query, envelope prefix, tombstone rule, or reconciliation with current API Secrets is supplied.
3. **Not mechanically checkable as written.** “No plaintext copy exists on persistent storage” is an unbounded negative and literally conflicts with retained historical backups. Define paths/filesystems/generations and search scope.
4. **Mostly observable after specification.** Issuer refs, URLs, Secret hashes, and certificate chain are checkable; “serves” needs a URL, trust method, expected status/body, and timeout.
5. **Partly observable.** Manifest fields are checkable; “correct probe classes” and “still works” remain human judgments without failure injections and expected results.
6. **Observable.** A marker/hash, PVC deletion, PV state, rebind, and restored hash can be scripted. The plan still needs the exact sequence.
7. **Partly observable.** CUDA output and concurrent encoded artifacts can be checked, but “transcode” and “failure of one consumer” need commands and expected non-impact. Do not inject a GPU reset/OOM beside live Docker consumers.
8. **Not mechanically checkable.** It does not name the failure, signal, alert destination, or detection deadline.
9. **Partly observable.** Replica readiness is mechanical; “ingress serving” requires an enumerated probe set and expected TLS/status/body.

## What v2 added that is wrong

- “One empty namespace” and “confirm it owns nothing” are false because auto-deploy creates an AddOn and Kubernetes creates a default ServiceAccount.
- A.3 cannot be the first executable state change while A.2 precedes it and reapplies live manifests.
- The A.3 rollback is ineffective while its manifest/AddOn remains.
- PC2 requires a scratch restore before every window while §9 defers that same restore drill.
- “Ledger row owner” refers to a nonexistent field.
- “Requests are absent from every existing manifest” is factually false.
- “Startup for init/migration” confuses probes with init containers/Jobs.
- Pod Security “named exceptions” are described as if they were per-workload despite namespace-level labels.
- “Any later deviation” from PC1 is too broad for a live controller-driven cluster.
- “Rotate” for historical plaintext is dangerously ambiguous; only credential rotation/revocation addresses copied plaintext.
- D.3’s generic “remove … path” is risky for a mounted ZFS dataset. Cleanup must name and destroy/unmount the dedicated dataset, not recursively delete an ambiguous path.
- P1E’s “failure of one consumer” is unbounded; GPU reset/fault injection can affect the live Docker consumers precisely because time-slicing provides no fault isolation.

## Minimum changes before executing P1A

1. Resolve PC2: either complete and record the scratch restore proof, or scope PC2 explicitly to P1B instead of “before ANY window.”
2. Make the namespace canary the actual first state change. Move A.2 later and deploy a validated, single-document Namespace manifest without rewriting the existing manifest set.
3. Define exact preflight validation: one document, `kind: Namespace`, name `migration-canary`, permitted labels only, unique valid AddOn filename, and unchanged hashes/specs for existing manifest-owned resources.
4. Replace the rollback with source-file removal, stale destination cleanup, AddOn deletion/disable handling, namespace deletion, and a non-recreation check.
5. Rewrite PC1 as a mechanical semantic diff with explicit allowed additions and exact abort conditions.
6. Split A.6 into a later substep before the first Pod; specify concrete NetworkPolicies and PSA label levels/versions, and choose namespace separation for privileged exceptions.
7. Add a real Owner field/mapping to the ledger and define the resource-measurement window/headroom rule.
8. Correct the probe contract and add a measurable CoreDNS capacity/failure criterion before any DNS-dependent canary.

After those changes, the bare namespace canary can be a safe first window. As v2 stands, it only appears isolated; the actual delivery and rollback paths are not.
