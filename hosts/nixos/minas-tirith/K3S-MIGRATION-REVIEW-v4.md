# Codex review — K3S-MIGRATION-PLAN.md v4

gpt-5.6-sol · effort high · 2026-08-06

## Verdict

**The D8 reversal is correct, but v4 is still not executable as written.** The proposed WAN-spanning three-server cluster has no credible third member. Do not resurrect it using the current hosts.

However, v4 confuses **disaster recovery** with **high availability**. A nightly SQLite backup makes the server potentially recoverable; it does not keep the API available, prove recovery, or define acceptable RPO/RTO. The restore drill must remain a hard gate.

## Is the D8 reversal correct

**Yes, for the available hardware. No, reason (d) is not sound as written.**

- Reason (a) reaches the right operational conclusion, although “not supported” is overly categorical. The decisive problem is putting quorum on WAN links and unreliable members, not whether Tailscale technically provides private reachability (`hosts/nixos/minas-tirith/K3S-MIGRATION-PLAN.md:217`).
- Reason (b) is merely implementation debt. The module assertion, firewall ports and token allocation can all be changed; they are not architectural reasons to reject HA (`modules/nixos/fleet/k3s-node.nix:81`, `modules/nixos/fleet/k3s-node.nix:145`).
- Reason (c) is decisive. An intermittently powered member is unacceptable, and osgiliath has not been demonstrated as a stable quorum member (`hosts/nixos/minas-tirith/K3S-MIGRATION-PLAN.md:225`).
- Reason (d) should say **“HA does not solve worker/data locality”**, not “HA buys little.” HA would not save minas-pinned data after minas fails, but it would preserve the API, scheduler and controllers during an independent pelargir failure.

Existing pods on minas may keep running during a control-plane outage, but the cluster cannot reconcile changes, reschedule failed pods, update endpoints, rotate Secrets, renew certificates, or execute controller-driven rollback. The backup path itself calls `kubectl` to scale workloads and therefore depends on the sole API server (`hosts/nixos/pelargir/backup.nix:4`, `hosts/nixos/pelargir/backup.nix:6`). With Traefik only 1/1, pelargir loss may also remove the shared ingress backend despite svclb being 2/2.

The correct conclusion is: **accept single-server operation with an explicit recovery SLA, or acquire two additional reliable co-located servers.** Do not fake HA with the current WAN hosts.

## Internal consistency defects

1. **Acceptance criterion 9 is abandoned-design debris.** “Kill one server, cluster stays writable” and “restore etcd” are impossible for the chosen single-server SQLite topology (`hosts/nixos/minas-tirith/K3S-MIGRATION-PLAN.md:384`). Replace it with a full single-server-loss and scratch-restore drill.

2. **D8 says recovery is DONE while P0.3 says the control plane is unrecoverable.** The restore drill is not done, so “DONE” and “restore 22 MB onto new hardware” are unjustified (`K3S-MIGRATION-PLAN.md:234`, `K3S-MIGRATION-PLAN.md:301`).

3. **P0.3 still says “etcd snapshots” and cites obsolete evidence.** This is SQLite, and the cited `backup.nix:63` now validates the SQLite backup rather than showing its absence (`hosts/nixos/pelargir/backup.nix:56`).

4. **“Phase 0 — ALL blocking” contains explicitly non-blocking, dropped and deferred entries:** P0.1, P0.2, P0.9 and P0.11 (`K3S-MIGRATION-PLAN.md:298`).

5. **The live-state section is stale.** It says pelargir is untainted, minas has one k8s workload and Docker has 35 containers (`K3S-MIGRATION-PLAN.md:72`). Today pelargir is tainted, minas has zero k8s containers, and Docker has 32. The deployed taint is visible in `hosts/nixos/pelargir/k3s.nix:22`.

6. **PinCollector remains in active phases after being parked.** D2 still counts model-service as a GPU consumer, Phase 4 still migrates it, and Phase 5 still says all three PostgreSQL clusters (`K3S-MIGRATION-PLAN.md:142`, `:335`, `:344`). The active scope now has two GPU consumers and two PostgreSQL bundles. The 35-row ledger also predates the stop and still presents the three PinCollector containers as active (`hosts/nixos/minas-tirith/K3S-MIGRATION-LEDGER.md:3`, `:31`).

7. **“Migrate ALL services” conflicts with the parked and excluded workloads.** Define this as “all currently running, non-parked services in the migration ledger” (`K3S-MIGRATION-PLAN.md:17`, `:93`, `:118`).

8. **D3 says no redesign, but Phase 1 still schedules a cert/reflector redesign** (`K3S-MIGRATION-PLAN.md:148`, `:321`).

9. **The Phase 0 effort still includes the osgiliath build**, despite P0.1 being decoupled and non-blocking (`K3S-MIGRATION-PLAN.md:360`).

10. **The HA revisit condition is arithmetically wrong.** One new co-located host beside pelargir produces two servers, not three; two additional co-located servers are required (`K3S-MIGRATION-PLAN.md:240`).

11. **D1’s deletion-protection requirement has no scheduled deliverable or acceptance test.** The only StorageClass currently has `reclaimPolicy: Delete`, yet Phase 1 does not explicitly require a Retain-class/protection mechanism (`K3S-MIGRATION-PLAN.md:137`, `:321`).

## New risks from abandoning HA

The plan needs compensating controls it currently lacks:

- A scratch restore from the actual offsite restic snapshot onto replacement hardware, using the recorded k3s version, flags, SQLite datastore and matching token; validate API access, Secrets, node rejoin, controllers and workloads. SQLite integrity and Kine row count are useful but do not prove bootable recovery (`hosts/nixos/pelargir/backup.nix:56`).
- A stated and measured control-plane RPO/RTO. A daily backup implies nearly 24 hours of cluster-state loss in the worst case.
- A tested bootstrap chain for replacement hardware: spare Pi/NVMe or named substitute, install media/Nix revision, sops identity, restic credentials, server token and operator access. Recovery must not depend on secrets available only on the dead pelargir.
- External API and backup-freshness monitoring. Backup and weekly repository checks cleanly skip when minas is unreachable (`hosts/nixos/pelargir/backup.nix:89`, `:153`); a skip must eventually become a stale-backup alert hosted somewhere other than pelargir.
- A pelargir-loss drill defining what continues without the API: Traefik, CoreDNS, ServiceLB, existing pods, endpoint changes, application backups, certificate renewal and Secret rotation.
- Redundant placement for shared data-plane components such as Traefik/CoreDNS where practical. Single-server control plane need not also mean single-replica ingress.
- Pre-upgrade checkpoints and explicit maintenance windows: every pelargir reboot or k3s upgrade is now a control-plane outage.
- An additional immutable/offline copy or independently protected retention. A backup on minas protects against Pi loss, but not credential compromise, operator deletion, or corruption propagated into the repository.

## Phase 0 completeness audit

**P0.3 is falsely treated as complete.** Capturing and validating `state.db` plus the token is complete; recoverability is not. P0.3 explicitly requires a proven restore drill, and that drill has not occurred (`K3S-MIGRATION-PLAN.md:301`). Consequently D8’s replacement for P0.2 is not DONE either.

Per the supplied hardware evidence, accept P0.4–P0.8 and P0.10 as complete. P0.1 is legitimately non-blocking; P0.2 and P0.11 are legitimately dropped; P0.9 is deferred with PinCollector. P0.11a is not stated as complete and remains an explicit rollback prerequisite.

Secrets encryption being Disabled and `local-path` using Delete do not invalidate Phase 0, but they are unresolved Phase 1 gates. No Secret-bearing or PVC-backed production workload should migrate before they are fixed.

## Largest remaining risk

**The single largest remaining risk is that the entire replacement for HA rests on a restore that has never succeeded.** Until a fresh offsite snapshot boots a scratch control plane and passes functional validation, “single-server-recoverable” is only “single-server-backed-up.” That is the migration’s current stop condition.

After that drill passes, the largest execution risk becomes the stateful dependency-group cutovers, especially the PostgreSQL bundles—but that is second today.
