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
- ⛔ **Run the container as ROOT and drop to the database uid only for `sqlite3`, via
  `setpriv`** — do NOT run the whole init container as that uid. Cross-review caught the
  difference and it matters: the dump directory is root-owned, so a wholly-unprivileged
  init cannot promote or `chown` the artifact, and making that directory writable by an
  application uid weakens the backups it is meant to protect. This mirrors exactly what
  `system.nix` already does for the docker path, and for the same reason — root opening a
  WAL database creates root-owned `-wal`/`-shm` the app then cannot open.
- **Validation is three parts**: `integrity_check` = ok **AND** table count > 0 **AND**
  bytes above a floor. An earlier draft of this document demanded a byte floor while the
  sqlite code checked only the first two; both now agree. The forced-failure run produced
  `ok=ok tables=0 bytes=4096`, which is why all three are needed.

## The marker contract, and how it reaches the heartbeat

The initContainer writes `/storage2/backup/dumps/.<name>.status`:

```
success <iso8601> tables=<n> bytes=<n>
FAILED  <iso8601> <reason>
```

`system.nix` consumes it (implemented, all branches tested). Two properties are
load-bearing:

- The expected set is **DECLARED**, not inferred from which markers exist. A job that
  never runs writes no marker; inferring would read that as success forever.
- Success must **PARSE**, not merely fail to say FAILED. An empty or truncated marker is
  what a crash actually produces, and the first implementation accepted it. It now
  requires `^success ... tables=<positive> bytes=<above 4096>`.

## Workload requirements the pattern imposes

Beyond the manifest invariants in `K3S-HANDOFF.md`:

- **`serviceName` + a governing headless Service** — a StatefulSet requires it — *plus* a
  normal ClusterIP Service for consumers and the traefik route. Two Services, not one.
- **No `strategy: Recreate`.** That is Deployment syntax; StatefulSets replace the single
  ordinal serially and do not take it.
- **`automountServiceAccountToken: false`** on the workload Pod. Only the CronJob needs API
  credentials; the application must not carry a token it has no use for.
- **Suspend the CronJob while the workload is staged at `replicas: 0`**, or it will run and
  fail against a pod that does not exist, before cutover has even happened.
- **`concurrencyPolicy: Forbid`**, `--wait=false`, a **digest-pinned** kubectl image, and
  resources on the job as well as the app.
- The init image must be **built by this repository** (`dockerTools` is the natural fit)
  containing sqlite3, `setpriv`, coreutils, grep and a shell — and referenced by digest.
  Do not use an arbitrary public convenience image, and do not install packages at pod
  start; the proof-of-concept used `apk add`, which needs network during startup and is
  unacceptable for a real workload.

## ⛔ Applying this to jellyfin — the traps a translation walks into

Reviewed 2026-08-07; these are the findings that rejected the first translation.

1. **Remove jellyfin from the legacy sqlite loop in the SAME commit** that adds it to the
   quiesce expectation. Leaving it in is not merely redundant: after docker stops, that
   loop still sees the same host database, has no container to stop, waits 60 s against
   the k3s writer, fails — and marks **every nightly backup degraded** from then on.
2. **Drop `NVIDIA_VISIBLE_DEVICES` from the workload.** The deployed device plugin runs
   `DEVICE_LIST_STRATEGY=envvar`, so its allocation response *sets* that variable to the
   allocated device. A compose-inherited `all` duplicates or overrides the plugin's
   selection and defeats D2's accounting. This is the one place the standing "only
   compose-declared env" rule must be broken, and it should be commented as such.
   Keep `NVIDIA_DRIVER_CAPABILITIES`.
3. **`/dev/dri` can be dropped** for an NVENC configuration — NVENC/NVDEC/CUDA go through
   `/dev/nvidia*`, and `VaapiDevice` is inert while `HardwareAccelerationType=nvenc`.
   Acceptance must include an **HDR tone-map** transcode, not just easy H.264, because
   tone-mapping is the path most likely to reach for a render node.
4. **Memory sizing must account for tmpfs.** `/dev/shm:/data/transcode` becomes
   `emptyDir: {medium: Memory}` **with an explicit `sizeLimit`** — but tmpfs pages count
   against the container's memory cgroup, and `sizeLimit` caps filesystem capacity without
   reserving or adding memory. Sizing a limit from the idle working set alone is wrong;
   it must be peak application + maximum tmpfs occupancy + margin, measured under a real
   transcode.
5. **Container name stays exactly `jellyfin`** — `monitoring.nix` matches container names,
   not pod names, so `jellyfin-0` would page.
6. D7 requires **cpu, memory AND ephemeral-storage** requests, on the app, the init
   container and the CronJob.

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
