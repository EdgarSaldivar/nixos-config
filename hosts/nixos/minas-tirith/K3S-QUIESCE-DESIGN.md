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

The initContainer writes **`/storage2/backup/staging/<name>/.status`** — the service's own
staging tree, **not** the shared dump directory. Moved there when jellyfin was built, on
cross-review, for two independent reasons:

- **Trust.** The capture runs as root. Mounting the shared `dumps` directory into it would
  give that container write access to *every* service's dump — enough to delete them, or
  to forge a `success` marker for a backup that never ran. It now reaches nothing but its
  own tree.
- **Availability.** A `hostPath` the kubelet cannot resolve is rejected **before** the init
  script runs, so the fail-and-exit-0 contract below cannot rescue it. Every mount the pod
  needs must therefore be one the backup can lose without taking the *service* down. Losing
  the staging mount degrades the backup — the marker and the staged database both go
  missing, loudly — instead of refusing to start jellyfin.

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
4. **⛔ The transcode volume is an OPEN DECISION, not a translation.** Measured
   2026-08-07, and the measurements make this harder than it looked:

   | fact | value |
   |---|---|
   | `/dev/shm` on the host | **63 G** (half of 125 G RAM), currently **0 used** |
   | jellyfin `EnableThrottling` | **false** |
   | transcodes in the last 3 days of logs | **zero** — nothing to measure a peak from |

   Under docker jellyfin writes to the host's shared 63 G tmpfs, **outside any cgroup
   accounting**, and with throttling DISABLED it does not pace itself — it transcodes
   ahead as fast as it can. There is no recent transcode to derive a worst case from, so
   any number chosen now is a guess.

   The two candidates are a genuine tradeoff, not a right answer:

   - **`emptyDir: {medium: Memory, sizeLimit: N}`** — proper Kubernetes isolation, but
     tmpfs pages count against the container's memory cgroup and `sizeLimit` caps
     capacity *without* reserving memory. Too small and a large 4K transcode fails; too
     large and the limit stops protecting the node. With throttling off, the safe number
     is unknown.
   - **`hostPath: /dev/shm`** — preserves today's behaviour exactly, including the intent
     recorded in `containers.nix` ("kept transcode writes off a drive at 33% wear"), and
     sidesteps the cgroup interaction entirely. Costs the isolation: shared node state,
     survives Pod replacement, can accumulate stale files.

   **DECIDED 2026-08-07: use `hostPath: /dev/shm`.** Reasoning, so it can be overturned
   with evidence rather than taste:

   - It is the **faithful translation**. This migration's whole discipline is translate
     first, improve later as its own change — the same rule that preserved `animearr`'s
     `GUID` typo and `radarr`'s `TZ=PS`.
   - `emptyDir` with a *guessed* `sizeLimit` introduces a **new failure mode that does
     not exist today**: a large transcode failing partway. Migrating should not add
     failure modes.
   - The isolation it costs is largely theoretical here — single-tenant node, and
     jellyfin already has exactly this access under docker. `plex` shares the same mount,
     so the concern is not jellyfin-specific either.
   - ⚠️ The node has **no swap**. Under docker a runaway transcode already competes for
     the 84 G available with the OOM killer as the only backstop; `hostPath` keeps that
     unchanged rather than making jellyfin the first thing killed.

   **Revisit when there is data**: enable throttling (an application change, its own
   step), run a real 4K HDR transcode, measure peak `/dev/shm` occupancy, and only then
   move to `emptyDir` with a `sizeLimit` grounded in that number. Sizing from the 870 M
   idle working set — which is what was first proposed — is wrong either way.
5. **Container name stays exactly `jellyfin`** — `monitoring.nix` matches container names,
   not pod names, so `jellyfin-0` would page.
6. D7 requires **cpu, memory AND ephemeral-storage** requests, on the app, the init
   container and the CronJob.

## ⛔ DURABLE STATE MUST LIVE IN GIT, NOT IN A `kubectl` COMMAND

The pattern used by all 15 earlier migrations — ship the manifest at `replicas: 0`, then
`kubectl scale` to 1 at cutover — leaves the **manifest and the cluster disagreeing**, and
k3s auto-deploy re-applies a manifest whenever its file checksum changes **or the server
restarts**. The imperative value therefore survives only until the next edit to that file
or the next k3s restart, at which point the declared `0` is reasserted and the service goes
down with nothing in git to explain it.

Measured on the live cluster: every migrated manifest declares `replicas: 0` while its
Deployment runs at 1. Fifteen services are one k3s server restart away from this.

So jellyfin's cutover **raises `replicas` and clears `suspend` in git**, as a second commit,
rather than with `kubectl scale`/`patch`. For the CronJob this matters more than for the
StatefulSet: a reverted `suspend: true` produces no error at all, it simply stops taking
backups, and the marker staleness gate would not say so for two days.

## Findings from the jellyfin cross-review (2026-08-07) — all folded in

The scope was REJECTED on first review. What it caught, beyond the two durable-state
blockers above:

1. **A bare `sync` is a node-global availability hazard.** `sync(2)` flushes *every*
   mounted filesystem, including the 98 TB media pool, so dirty or failing I/O anywhere on
   the node blocks the initContainer while the application is completely absent — and an
   initContainer has no liveness probe or deadline to rescue it. It also *always* reports
   success, so it cannot detect the writeback error it exists for. Use `sync -d <file>`:
   bounded to one file, and it actually returns failure.
2. **`concurrencyPolicy: Forbid` turns one hung Job into a permanent outage of the
   backup.** Forbid skips every later schedule while an earlier Job is active, and
   `kubectl`'s default request timeout is **zero**. Needs BOTH `activeDeadlineSeconds` on
   the Job and `--request-timeout` on the call; `--wait=false` does not cover it.
3. **Deleting by stable NAME is not idempotent across Job retries.** Kubernetes permits a
   Job's program to run more than once. If the first DELETE commits but its response is
   lost, the controller may already have recreated the pod — and the retry deletes the
   *replacement*. Keep `backoffLimit` low; add `startingDeadlineSeconds` so unsuspending
   does not immediately fire missed schedules.
4. **`hostPort` is not a writer-exclusion fence.** CNI implements it as PREROUTING DNAT,
   not a host socket, so `docker start <name>` can still run the old container against the
   same database. Preserved as the reason jellyfin's cutover *removes* the docker
   container rather than relying on `profiles: ["migrated"]`, which the handoff already
   says is a guard against accident and not a fence.

Two findings were considered and **not** adopted, recorded so they are not silently
re-litigated:

- **"Reusing the application image crosses the backup trust boundary."** Correct in
  principle, and the reason the marker and staging moved out of the shared dumps directory
  — that change removes the escalation the finding described. What remains is a container
  that can write only its own service's staging tree, using an image that already runs as
  root on this node with hostPath access to the same database. A repo-built image would
  add real separation, but it would also put a **second image in jellyfin's startup path**,
  which is a new way for the service itself to fail to start. Revisit if in-repo image
  build/import infrastructure ever exists for another reason.
- **"No memory limit risks a global OOM on a swapless node."** Also correct, and it is
  today's docker behaviour exactly — jellyfin runs with no limit now, so the migration
  does not add it. Any limit would be a guess (`EnableThrottling=false`, no transcode in
  three days of logs), and the failure mode of guessing low is a 4K HDR transcode killed
  partway: a NEW failure mode, which is the same reasoning that chose `hostPath` over an
  `emptyDir` with a guessed `sizeLimit`. Both revisit on the same trigger — enable
  throttling, run a real transcode, measure peak `/dev/shm`, then set both numbers from
  the measurement.

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
