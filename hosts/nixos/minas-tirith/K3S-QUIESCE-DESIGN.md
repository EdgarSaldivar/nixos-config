# Quiescing a k3s-hosted database for backup — the proven design

**Status: mechanism PROVEN end to end on a throwaway workload, 2026-08-07. Not yet applied
to jellyfin.**

## The problem

`jellyfin` is the only service whose database cannot be dumped while it runs: `library.db`
is in **rollback-journal mode** (not WAL) and jellyfin holds it write-locked for its entire
runtime. A 60 s busy timeout was tried and still failed. Under docker the backup script
does `docker stop jellyfin`, dumps, restarts, with a trap and a CRITICAL marker if the
restart fails.

That does not translate, because **minas is a k3s AGENT node with no kubeconfig and no
API access at all** (verified: `k3s kubectl get nodes` fails). The backup script cannot
scale a Deployment. `crictl stop` is not viable either — the kubelet restarts the
container immediately, so the dump would race an uncontrolled restart.

## ⛔ Why the obvious design is wrong

The intuitive answer is a CronJob that scales the Deployment to 0, dumps, scales back.
**Do not do this.** If that job is killed after scaling down — SIGKILL, node failure,
OOM — `replicas=0` persists **forever**, and Kubernetes treats it as the correct desired
state. A shell trap does not run on SIGKILL.

This is strictly worse than the docker version it replaces: there, a crashed script leaves
a stopped container that the heartbeat notices and complains about. Here the controller
would *actively maintain* the outage. Any scale-to-zero design needs an independent
deadman to be safe, which is a second mechanism to get right.

## The design that works

Delete a **pod**, never change desired replicas.

1. Run the workload as a **single-replica StatefulSet**, pinned to minas.
2. A **CronJob** deletes the predictably-named pod (`<name>-0`).
3. The StatefulSet controller recreates it. **Desired replicas is always 1**, so a job
   killed at any point is self-healing — there is no state that expresses a durable outage.
4. The pod's **initContainer** takes the backup. It runs before the app container, so the
   writer is absent *by construction* rather than by stopping anything.

### RBAC — and why StatefulSet specifically

```yaml
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "delete"]
  resourceNames: ["<name>-0"]
```

A StatefulSet's **stable pod name** is what makes `resourceNames` expressible. A
Deployment's random suffix would force `list` + `delete` across the namespace.

Verified with `kubectl auth can-i --as=system:serviceaccount:...`:

| capability | result |
|---|---|
| `delete pod/<name>-0` | **yes** |
| `delete pod/<other>` in another namespace | no |
| `delete pods` namespace-wide | no |
| **`patch deployments/scale`** | **no** |
| `list pods` | no |

No credential leaves the cluster, and no scale permission exists anywhere.

## Three defects found by testing, not by reading

1. **`kubectl delete --wait=true` needs `list`/`watch`.** It sets up a watch, which the
   narrow RBAC correctly denies, so the job hangs forever — while the delete itself has
   already succeeded. Use **`--wait=false`**. Do not widen RBAC to fix this.
2. **The quiesce window is `terminationGracePeriodSeconds` long.** Measured: `Killing` at
   02:54:17, `SuccessfulCreate` at 02:54:47 — 30 s of default grace. Verification that
   samples inside that window sees the OLD pod still `Running`, because a terminating pod
   reports `Running`. Wait for the pod **UID to change**, not for phase.
   For jellyfin the grace period should be generous, so it shuts down cleanly and
   releases the DB lock rather than being killed holding it.
3. **`integrity_check` alone does not validate a dump.** The forced-failure run produced
   `ok=ok tables=0 bytes=4096` — integrity_check says **ok** on a perfectly valid,
   perfectly EMPTY database. Only the table-count and size gates caught it. Validation
   must be integrity_check **AND** table count > 0 **AND** a size floor, exactly as
   `system.nix` already does for the docker path.

## The initContainer contract

- Dump to `.tmp`, validate, then promote atomically. Never overwrite a good dump with an
  unvalidated one.
- On failure: **preserve the previous dump**, write a durable status marker, and
  **exit 0**. Availability must not be held hostage to its own backup. Proven: with the
  backup forced to fail, the app container still reached `Running`/`ready` and the prior
  good dump was untouched.
- Run as the database's owning uid (see the `setpriv` reasoning in `system.nix` — root
  creating `-wal`/`-shm` makes them unopenable by the app).

## What is still unproven — do these before jellyfin cuts over

- Termination ordering: does jellyfin actually release the lock within its grace period,
  or does it get SIGKILLed still holding it?
- A pinned image containing `sqlite3` for the initContainer (the probe used `alpine` +
  `apk add`, which needs network at pod start — unacceptable for a real workload).
- UID/GID handling against jellyfin's real config tree.
- **Freshness signalling into the existing heartbeat**: the marker files must be consumed
  by minas' backup script so a CronJob that silently stops running shows up as
  `degraded`/`.failed`, rather than as an absence nobody notices. The k3s dump-staleness
  walk added in `6c842de` is the closest existing hook.
- An actual **restore test** of a jellyfin dump produced this way.
