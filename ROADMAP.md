# Roadmap

Open work only. Anything finished is deleted from this file and lives in git
history — a backlog that lists completed items stops being read.

Last audited **2026-08-16** against the source, not against recollection. Items
marked ⏳ cannot be settled from this repository; each names the exact observation
that would settle it, because "we think it works" is how the last set of stale
entries happened.

---

## 1. ✅ DONE — the cluster is off `/home/edgar/git/docker`

**Completed 2026-08-16. The directory has been deleted.**

The docker→k3s migration had left the old Compose repository's *directory tree* in
place, still serving live workloads. Everything moved to `/usr/local/etc/<service>`:

| binding | resolution |
|---|---|
| immich config | relocated, [runbook](docs/runbooks/minas-tirith/relocate-immich-shelfmark.md) |
| shelfmark config + `users.db` (a backup dump target) | relocated, same runbook |
| traefik file provider — 25 generated route files | relocated, [runbook](docs/runbooks/minas-tirith/relocate-traefik-file-provider.md) |
| hand-maintained `traefik.yml` | deleted outright (item 11) — fully redundant, and it held a plaintext bcrypt credential |
| `.my_custom_proxy_settings.conf` | never ported; the path docker mounted was an empty *directory*, into an Apache image that has no nginx |

Verified after deletion: every Pod Running, and **26 of 27 baseline ingress rows
match exactly** (the 27th is `dungeon`, expected dead — item 11).

`checks/external-checkout-dependency.nix` is KEPT, at zero entries, as a regression
guard: the directory no longer exists, so any new reference to it is a bug that
should fail the build rather than fail at runtime.

✅ **The follow-on exposure is closed too.** Deleting the tree left 14
credential-shaped environment variables in docker's own metadata across 31 stopped
containers. Those containers were removed the same day; the count is now 0. See
item 12.

## 2. ✅ DONE — the embedded shell is extracted

**Completed 2026-08-16.** Both programs are now real files that ShellCheck and an
editor can read:

| | before | after |
|---|---|---|
| `backup-root-data.nix` | 1124 lines | **110** |
| `monitoring.nix` | 617 lines | **86** |
| `scripts/backup-root-data.sh` | — | 1050 lines |
| `scripts/healthcheck-ping.sh` | — | 553 lines |

Transformed by SCRIPT, not by hand or regeneration — dedent, `${pkgs.X}/bin/cmd` →
`cmd` (83 and 14 sites), `''${` → `${` (12 and 34), and the heartbeat's two sops
interpolations to a placeholder the module substitutes. The extractors assert every
count and refuse to write if any store path, interpolation or Nix escape survives.

**NOT `writeShellApplication`**, which this item originally named. It exports
`runtimeInputs` onto PATH from *inside* the script, which would put the real `zfs`,
`docker` and `k3s` ahead of the fakes the 22 fixtures inject — the suite would keep
passing while testing nothing. Resolution lives in `systemd.services.<n>.path`
instead, where the caller still controls it.

### The evidence, since this item said it could not be gated normally

- **Golden comparison.** The rendered program bodies before and after the change are
  **byte-identical** once the enumerated command normalisation is applied — the only
  differences are two ShellCheck annotations added deliberately. That is the
  equivalence proof this item said was unavailable; it was available, just not as
  byte-equality of the unmodified text.
- **`minas-unit-contract`** (new): the only line that changed in either rendered unit
  is its `PATH`. ExecStart, the other `Environment` lines, `Type`, `Nice`,
  `IOSchedulingClass`, `TimeoutStartSec`, `StateDirectory`, `OnFailure` and
  `Description` are unchanged.
- **`minas-shell-lint`** (new): ShellCheck + `bash -n` + Nix-residue and whitespace
  guards. This is what makes the extraction *worth* doing — moving to `.sh` only made
  linting possible.
- **`minas-command-resolution`** (new): pins the command → package mapping for both
  programs and, on x86_64-linux, performs real PATH resolution.
- **22 fixtures** pass against both extracted programs.

⚠️ **The heartbeat's PATH gained `rasdaemon`, `sqlite` and `systemd`.** Those three
were interpolated store paths and so never needed to be on PATH. Dropping any one
now reproduces the `gawk` failure this repo has already been bitten by: `command not
found` on stderr, unit exits 0, heartbeat still pings OK, and the check silently
stops checking. That is why `minas-command-resolution` exists.

## 3. Heartbeat signal quality — the false positives are fixed

Found 2026-08-16 by tracing an actual heartbeat run. **The alerting works
correctly** — it POSTs `UNHEALTHY: …` to `/fail` with the full problem list. The
problem was signal quality: false positives drowning real signals.

| reported | status |
|---|---|
| `k8s pods NOT Ready` ×3 | ✅ **FIXED.** See below — and it was worse than recorded. |
| `DEGRADED dumps` ×3 | ✅ **RESOLVED** by deleting the 31 stopped containers (item 12). The walk is over `docker ps -a`; there is nothing left to walk. |
| `zpool: pool: storage` | ⏳ **REAL.** A scrub is running as of 2026-08-16 18:04. `0B repaired`, one permanent error `storage:<0x41080>` with no filename — likely a deleted file still held by a snapshot. Re-check when it finishes. |
| `SCRUTINY DISK ALERT … nvme0` | ✅ Acknowledged; the latch was cleared. Watch the counter. |

### The pod-readiness check had TWO defects, not one

The recorded complaint was that `Completed` Job pods were counted as unhealthy.
True, but the same one-line check was also **mis-parsing its input**:

```
k3s crictl pods | awk 'NR>1 && $5!="Ready" {print $6}'
```

`CREATED` is human-readable prose of **variable width**. "2 hours ago" is three
fields; "About an hour ago" is four. So for any pod in its second hour every column
shifted, `$5` was `ago`, and `$6` was the literal word `Ready` — **the heartbeat was
alerting on a pod named "Ready"**. Verified against live output, not inferred.

Replaced with `crictl pods --state notready -q`, which filters server-side and
returns IDs, so there are no columns to mis-parse; names and labels come from
go-template, the mechanism the tunnel probe already used. Job pods are excused only
when they actually **succeeded** — a failed Job is still reported, as
`name(job-failed)`.

Fails closed throughout, which matters because this program has **no**
`set -euo pipefail`: an unreadable exit code or a Job sandbox with no containers is
reported, not silently treated as success. That is the same shape as the missing-`gawk`
bug that made a check silently stop checking.

Against the live cluster: **4 reported problems → 0**, with the three Completed Jobs
correctly excused. Eight fixtures added, mutation-proven (removing the Job exclusion
fails; making the exit code fail-open fails).

### Still open

⏳ **Confirm the degraded marker clears.** `/var/lib/backup-root-data.degraded` still
names `infra-postgres-1`, `immich-postgres14` and `nextcloud-db`. It is a cache
written by the nightly backup, and it predates the container deletion. The next run
takes the `else rm -f` branch and removes it. Deliberately NOT deleted by hand, so
that this is a verifiable prediction rather than a cover-up.

✅ **Three stale dump artifacts deleted 2026-08-16**, each only after its live
successor was verified present and fresh in the same operation:

| deleted | superseded by |
|---|---|
| `nextcloud-db.sql.gz` (7d) | `k8s-nextcloud-nextcloud-db.sql.gz` |
| `immich-postgres14.sql.gz` (7d) | `k8s-immich-immich-postgres14.sql.gz` |
| `_usr_local_etc_jellyfin_config_data_data_library.db` (8d) | `_storage2_backup_staging_jellyfin_current_library.db` |

⛔ **Correction: `infra-postgres-1` DOES have a successor.** This document previously
said it was "parked with no k3s successor". That was wrong, and it is worth recording
why, because the name gives no hint: `infra-postgres-1` was the **PinCollector**
database, running in the `infra` compose project. Its successor is
`k8s-pin-collector-postgres.sql.gz`, dumped daily.

Established by comparing the dumps rather than by reading the name — both are
`pg_dumpall` cluster dumps of 3925 lines, one role (`pin_collector`), one database,
**52 tables and 52 COPY blocks each, with the same 5 data rows**. The only real
differences are the role's password hash and PostgreSQL's rendering of CHECK
constraints, which differs between the docker-era and k3s server versions.

So it is redundant, not irreplaceable. It is nonetheless still on disk, because
"delete the only copy of a database" deserves an explicit decision even once the
evidence says it is not the only copy.

⚠️ **Unrelated but noticed:** `_usr_local_etc_shelfmark_config_users.db-wal` and
`-shm` are being dumped as if they were databases. They are SQLite sidecar files.
Harmless, regenerated every run, but the dump walk should not be treating them as
database sources.

### Two related bugs, still unfixed

- ⛔ **Tracearr's dump age is never checked.** The freshness walks cover `.sql.gz`
  and `.sql.gz.age`; tracearr deliberately produces `k8s-media-tracearr.dump`
  because its `.sql.gz` form is **not restorable**. Its existence is accepted but
  its staleness is invisible.
- **The never-created check accepts any of three extensions** for every database
  rather than the exact expected format, so an obsolete wrong-format artifact can
  satisfy existence. Worst precisely for tracearr.

### And the general shape of it

Artifact names derive from source paths or container names, and **nothing prunes an
artifact when that name changes at migration**. This bit shelfmark (its relocation
created a new dump name; the old one had to be deleted by hand), produced the docker
false positive, and left the four artifacts above. Orphan retirement wants a rule —
probably "an artifact whose source no longer exists is reported once, then retired
on an explicit list" — not another special case.

## 4. Decide the osgiliath staging question

Osgiliath is **not deployed** — still on docker/ubuntu. But `pelargir/manifests.nix`
delivers five osgiliath manifests to the live cluster, and four of them declare
`replicas: 1` with `nodeSelector: kubernetes.io/hostname: osgiliath`. With no such
node they are permanently Pending.

Either gate them to zero until the host exists, or accept and document the Pending
state so it does not read as a fault later. Right now it is neither.

## 5. ✅ DONE — stale k3s objects are now reported

**Completed 2026-08-16.** `hosts/nixos/pelargir/k3s-reconcile.nix` runs weekly and
reports what the cluster is running that no manifest declares any more.

Removing an object from a manifest — or removing a manifest file entirely — still
does not delete the applied resource, and that stays deliberate: `manifests.nix`
only *reports* a stale file because deleting during activation could drop a live
route. What is closed is the gap that **nothing tracked what had accumulated**.

### How it knows

Every object k3s applies carries `objectset.rio.cattle.io/owner-name: <addon>`, and
each AddOn records the **kinds** it manages (`addon.k3s.cattle.io/gvks`) — kinds, not
objects, which is precisely why k3s cannot prune on its own. So for each AddOn:
parse the file it was applied from, list live objects of those kinds, keep the ones
it owns, and compare both ways.

- **ORPHANED** — live, owned, no longer declared. The accumulation being hunted.
- **MISSING** — declared, not live. Failed apply, or deleted out of band.
- **GAPS** — a kind that could not be listed or a file that could not be parsed.
  Reported separately and the only condition that exits non-zero, because a report
  that does not know what it does not know must not read as "all clear".

Findings do **not** fail the unit. Orphans are normal for hours after a deliberate
removal, and a unit that goes red for something expected is a unit whose red gets
ignored.

### It found two real things on its first run

✅ **A stale AddOn owning four live secrets — REMEDIATED 2026-08-16.**
`pelargir-home-secrets` had `spec.source` pointing at a file that no longer exists —
the rendered Secret manifest stopped being installed there when secrets moved to
tmpfs application via `k3s-apply-secrets` — yet it still claimed
`cert-manager/cloudflare-api-token`, `home/ddns-updater-config`,
`home/mosquitto-auth` and `home/zigbee2mqtt-config`.

⛔ **The order was the whole risk**, and it is worth recording: disown the objects
FIRST, delete the AddOn SECOND. Deleting the AddOn while it still claimed them is
how you lose cert-manager's Cloudflare token, mosquitto's password and the Zigbee
network key in one command.

Done as:

1. Confirmed the four are regenerable — all are declared in
   `sops.templates."pelargir-home-secrets.yaml"`, so `systemctl restart
   k3s-apply-secrets` restores them. That was the safety net, established before
   touching anything.
2. Captured each secret's `.data` hash.
3. Stripped every `objectset.rio.cattle.io/*` annotation and the matching label.
4. Verified the AddOn claimed **zero** objects before deleting it.
5. Deleted the AddOn; re-verified all four present with **identical** hashes.
6. Confirmed ddns-updater, home-assistant, mosquitto, zigbee2mqtt and all three
   cert-manager pods Running with 0 restarts, and that restarting
   `k3s-apply-secrets` still reconciles them (it uses plain `kubectl apply`, so it
   does not re-add the objectset ownership).

✅ The other finding — `MISSING Middleware/home/cloudflare-only` — was correct and
expected: item 7 deleted that object from the cluster, and pelargir has not yet been
rebuilt, so the on-host file still declares it. It will clear on the next deploy.

## 6. ✅ DONE — manifests are schema-validated

✅ **Object identity** — `checks/manifest-objects.nix` asserts no two manifests
declare the same `(apiVersion, kind, namespace, name)`, the collision that makes two
k3s AddOns fight over one object. Build-time, no cluster needed.

✅ **Schema** — added 2026-08-17 to `k3s-reconcile`, which server-side dry-runs every
delivered manifest. Result on the live cluster: **60 addons, 0 invalid**. Verified it
actually catches things: a Deployment with `contaienrs` misspelled is reported as
`unknown field "spec.template.spec.contaienrs"`.

### Why the API server rather than the vendored kubeconform this item asked for

The original plan was a pinned kubeconform pass with a schema bundle vendored into
the store, since the check sandbox has no network. That is buildable but the API
server is the better tool here, not merely the easier one:

- **It knows the CRDs.** This fleet uses `traefik.io/v1alpha1` TLSOption,
  `cert-manager.io/v1` Certificate and ClusterIssuer, and `helm.cattle.io/v1`
  HelmChart — 5 of the 22 kinds in use. A vendored bundle covers the 17 core kinds
  and needs hand-maintained CRD schemas plus a refresh ritual every time a CRD is
  upgraded. The cluster serves all of them, at exactly the installed versions
  (verified: 552 KB of traefik schema, 528 KB cert-manager, 140 KB helm).
- **It adjudicates immutable fields**, which this item correctly noted nothing
  offline can decide.
- **It is the same authority that will reject the manifest for real**, so a pass
  here means something a generic bundle cannot promise.

⚠️ **The trade, stated honestly:** this needs a cluster, so it is a periodic and
pre-deploy gate rather than a `nix flake check` gate. A bad manifest can still be
committed; it is caught before it silently fails to apply, not before it is written.
For a fleet that always has a cluster in reach that is the better half of the trade —
but if this repo ever needs cluster-free CI validation, vendored kubeconform is the
thing to add, and it would complement rather than replace this.

`INVALID` findings fail the unit, unlike orphans and MISSING. A manifest the API
server refuses means an object silently is not there, which is never an expected
steady state.

## 7. ✅ DONE — the Cloudflare list is one source

**Completed 2026-08-16.** The ranges now live only in
`hosts/nixos/pelargir/cloudflare-ranges.nix`, and `checks/cloudflare-ranges.nix`
fails the build if a literal reappears anywhere under `hosts/`.

⛔ **There were THREE copies, not two.** This item — and the check that claimed
"divergence is now impossible" — only knew about two. The third was minas' traefik
`--entrypoints.https.forwardedHeaders.trustedIPs`, and it was found by rewriting the
check to police the whole tree rather than compare a known pair.

| copy | outcome |
|---|---|
| pelargir `wireguard.nix` nftables gate | now imports the shared list |
| pelargir `manifests/ingress.yaml` ipAllowList | **deleted** — see below |
| minas `manifests/traefik.yaml` trustedIPs | now substituted from the shared list |

### The ipAllowList was not duplication, it was a footgun

`cloudflare-only` **could never work here**: the UniFi router SNATs forwarded
traffic, so Traefik sees the router's address rather than a Cloudflare edge IP and
no range ever matches. Verified 2026-08-05 — legitimate requests got 403, the same
request without the ACL got 200. mTLS replaced it and *is* wired up, through the
Home Assistant Ingress's `router.tls.options: home-cloudflare-mtls@kubernetescrd`.

So it was a live object named `cloudflare-only`, referenced by nothing, that would
silently 403 Home Assistant the moment anyone pointed a route at it believing it was
the origin protection. Removed from the manifest and deleted from the cluster —
k3s's AddOn carries no tracked object list, so removing it from the file alone would
have left it live.

### Why this could be done without the window the item asked for

The concern was that generating the manifest changes how it is delivered. It does
not: the AddOn `name` (`minas-traefik.yaml`) is unchanged, so the frozen basename
and AddOn identity are untouched — only the file *content* is produced differently.
And it is produced **byte-identically**: the rendered nftables rules hash the same
before and after, and every non-comment byte of the traefik manifest matches, the
`trustedIPs` line included.

### One real question this surfaced, deliberately not answered

⚠️ **`trustedIPs` carries the 15 v4 ranges but not the 7 v6 ranges.** That asymmetry
predates this change and is preserved verbatim, because fixing it is a behaviour
change rather than part of removing duplication. If Cloudflare ever reaches the
origin over IPv6, those requests' `X-Forwarded-For` would not be trusted and the
client IP in logs and any downstream ACL would be the connecting address instead.
Decide it on purpose.

## 8. ✅ RESOLVED — the admin key is backed up off this machine

`.sops.yaml` still declares exactly one human identity, and that is fine now that
the identity itself survives losing this Mac.

**The risk was never "one recipient", it was correlated loss.** `admin_edgar` is a
recipient of all six secrets files, so the admin key alone decrypts everything —
but that key lived only on this Mac, alongside the staged host keys. One stolen or
dead laptop took out every copy at once. `.sops.yaml` records that pelargir's
original host key already died with an SD card, so it is not a hypothetical.

Resolved 2026-08-17 by backing the keys up to a password manager — a genuinely
different failure domain — rather than by adding a second recipient. That was the
simpler correct fix: adding a recipient means `sops updatekeys` across every file
and a redeploy, and solves the same failure. Five files are backed up:

```
~/.ssh/id_ed25519            derives to admin_edgar AND logs into the hosts
~/.config/sops/age/keys.txt  the same key in age form, what sops reads by default
~/.ssh/saldivar.io_ed25519   reaches osgiliath and saldivar.io
~/Development/secrets/{minas-tirith,pelargir}/ssh_host_ed25519_key
```

The two host keys are not needed to DECRYPT — the admin key covers all six files —
but without them a reinstall becomes rekey-every-file-and-redeploy.

⚠️ **The one remaining dependency is the password manager's own recovery.** If its
Emergency Kit exists only on this Mac, the problem has just moved up a layer. Keep
that offline.

A second human recipient is still the stronger answer if this ever stops being a
one-person fleet — it survives the admin being unavailable, not merely the admin's
laptop dying. Not needed today.

## 8b. Credential hygiene fallout from the 2026-08-16 secrets audit

Two items the audit surfaced. Neither is fixed by `chmod` or by deleting the legacy
checkout — only rotation at the issuer closes them.

⛔ **A Cloudflare GLOBAL API KEY sits in `/home/edgar/git/docker/backup.yaml`** (37
chars, all hex — the global-key shape, not a scoped token). It is commented out and
has no uncommented consumer anywhere. **Commented out is not revoked.** If it is
still valid it grants full account access — every zone, unscopeable — and it
predates the scoped token now in sops. Revoke or regenerate it in the Cloudflare
dashboard, after checking for consumers outside this repo, since rotation breaks
anything else using that same identity. The scoped
`traefik_cloudflare_dns_api_token` is the replacement and is unaffected.

⚠️ **`palworld_admin_password` is a reused personal password.** It is the same
10-character string that signed tracearr's JWT and cookie until they were rotated,
and that appears in the legacy checkout as a WireGuard `PrivateKey`. A full sweep of
all 53 keys across all six sops files found this as the only surviving instance.
Palworld's REST admin surface (8212) is deliberately unpublished, so this is not
internet-facing and is not urgent.

The important part is not the palworld rotation. **If that string is used for
anything outside this fleet, it must be changed there** — its appearance in four
unrelated roles is the signature of a password reused from elsewhere.

## 9. deploy-rs

Magic rollback: activate, and if the host does not confirm connectivity within a
timeout it **reverts automatically**. For a machine an hour away, where a bad
switch means a drive, that is the single most valuable piece of tooling available.
Colmena is fleet machinery; four hosts do not need it.

## 10. Renovate

Input-update PRs — now that evaluation CI exists to validate them.

## 11. `dungeon.saldivar.io` — routing retired, DNS deliberately kept

✅ **Done at the traefik layer, 2026-08-16.** Its four routers and three services
pointed at `192.168.6.94`, a dead host, and returned `000`. They lived in a
hand-maintained `traefik.yml` inside the file-provider directory — now deleted,
which also took a plaintext bcrypt credential out of that directory.

Verified before and after: the full external ingress acceptance from pelargir
passes against the recorded baseline, and `traefik.saldivar.io` still redirects to
authentik. The dashboard was never served by that file — the generated
`k8s-authentik-gate.yml` routes it to `api@internal` at priority 9000, shadowing the
hand-maintained router entirely.

Rollback: `/var/tmp/traefik.yml.disabled` and a timestamped backup beside it.

⛔ **The DNS record STAYS. Do not "retire" it.** Confirmed with the operator
2026-08-17: dungeon is an ongoing project that is simply not deployed right now, not
an abandoned one. Earlier revisions of this item proposed deleting the record; that
was wrong and would cost a re-provision when the project resumes.

Consequence to leave alone: the hostname resolves and fails at TLS, so the ingress
acceptance baseline lists it as an expected `000ERR`. That row is CORRECT and should
stay until dungeon is actually deployed, at which point it becomes a real status.

## 12. ✅ DONE — docker is decommissioned on minas

**Completed 2026-08-17.** ⚠️ **nardol still runs docker for wolf — this was always
minas-only.** Wolf mounts the docker socket and spawns a container per game session
from ~11 pinned images; moving it is a migration project, not cleanup, and buys
nothing on a separate host.

| step | result |
|---|---|
| 31 stopped containers | removed 2026-08-16, taking 14 plaintext credential env vars with them |
| ~80 GB of images | reclaimed 2026-08-16 |
| 61 named volumes, 3.3 GB | reclaimed 2026-08-17, **after** verifying file-count parity against the backup |
| `virtualisation.docker` | removed — `containers.nix` 246 → 152 lines |
| dead branches in the two programs | removed or ported (below) |

`/var/lib/docker` is down to 391 MB of daemon metadata from ~84 GB.

### What stayed, and why removing it would have broken things

⛔ **`hardware.nvidia-container-toolkit` is NOT docker.** On NixOS it is CDI-only: it
emits the spec at `/var/run/cdi/nvidia-container-toolkit.json` that `k3s-gpu.nix`'s
containerd runtime shim reads. Removing it because the name contains "container"
takes GPU transcode away from Plex and Jellyfin, which are now Pods.

⛔ **The SQLite quiesce mechanism is left in place, inert.** It stops the service
holding a database before dumping it. It cannot fire — every entry in the list has an
empty stop-container field, *and* docker is gone — but deleting it removes a
**capability**, not dead weight. The k8s equivalent is scaling a Deployment to 0 and
back, which is a design decision with its own failure modes. Ripping ~130 lines of
stop/trap/restart logic out of the fleet's most important program to delete something
that provably never executes is the worse trade.

### The heartbeat: one check ported, one lost on purpose

✅ **Crash-loop detection was PORTED, not deleted.** It read
`docker inspect -f '{{.RestartCount}}'`; removing it with the daemon would have
silently deleted the check. containerd's equivalent is `metadata.attempt`, parsed
from `crictl ps -a -o json` with `jq`.

Two things that only showed up by testing against the live runtime first:

- `crictl ps -a` lists **every attempt as its own row**, so one name appears many
  times — measured: `verify-pgdata 118` beside `verify-pgdata 117`. Docker names were
  unique. Without `group_by`/`max_by` the delta is computed against whichever
  duplicate is read first: wrong, and unstable between runs. 63 rows collapse to 49
  names.
- parsed as **JSON, never the `crictl ps` table**. That table has no `--template` and
  its CREATED column is variable-width prose — column-position parsing there is
  exactly the bug that had this heartbeat alerting on a pod named `Ready`.

⚠️ **COVERAGE LOST, recorded rather than glossed:**
`docker ps --filter health=unhealthy` has no containerd equivalent. Docker HEALTHCHECK
is a runtime concept; in Kubernetes the readiness probe plays that role and the
kubelet acts on it by pulling the Pod from Service endpoints, not by exposing a state
a runtime query can see. `k8s_unhealthy_pods()` is the nearest replacement and is
**not equivalent** — it sees sandbox state, not per-container readiness, so a Pod
whose readinessProbe is failing still looks Ready to it. That gap is why the two VPN
tunnels have their own behavioural probe.

`docker ps --filter status=restarting` was also dropped, with no loss: containerd has
no "restarting" state because the kubelet owns restart backoff, and the attempt-delta
check catches a crash loop whatever state the container is caught in.

### Still open

⏳ **`/var/lib/resource-samples/samples.csv`** on minas is NOT in the backup source
set and is the empirical basis for every resource request in the manifests. Preserve
it deliberately or discard it deliberately.

## 13. Namespace-level NetworkPolicy

Default-deny was deferred "until Phase 6" because traefik was then an external
docker container. It is now an in-cluster Pod, and `books`, `media`, `games`,
`nextcloud` and `immich` still have no default-deny policy. Only authentik and
pin-collector define one.

---

## Open architectural decisions

**pelargir is a cluster-wide control-plane single point of failure.** One SQLite
control plane, in one house. If the Pi is down, minas' data plane may keep serving
best-effort but there is no API, no reconciliation, no cert-manager, no reliable
restart or reschedule.

Adding minas as a second control-plane member does **not** fix this — a two-member
quorum is not fault-tolerant. If minas must survive a long pelargir outage, the
honest architecture is **one cluster per house**, which costs a second control
plane and duplicated operations but matches the physical failure domains, and
loses little scheduling flexibility because the workloads are already immovable.

Recorded because it is larger than any item above and should be decided
deliberately rather than discovered during an outage.

---

## Verify on the hardware

Source cannot settle these. Each names what would.

- ⏳ **A nightly backup has actually succeeded with database dumps present.**
  `systemctl status backup-root-data`, the stamp file, and dump sizes on the pool.
- ⏳ **pelargir's off-host restic backup works end to end.** `restic-minas
  snapshots`, latest snapshot age, and a scratch restore. Both ends are declared in
  Nix now; that is not the same as a proven restore.
- ⏳ **nardol's cold boot has never happened.** The initrd unlock changes are inert
  until it reboots, and that must be done at the machine. `keyFileTimeout = 10` is
  a guess; slot 0 is unverified; the nine unlock drills are unrun; the USB stick is
  the only copy of that key.
- ⏳ **The rescue-microSD rollback has never been rehearsed.** A paper procedure is
  not hardware proof.
- ⏳ **The Pi RTC battery** must be commissioned before setting `dtparam=rtc_bbat_vchg`.

---

## 14. Reboots leave hostPath PostgreSQL uncleanly stopped

Found the hard way 2026-08-17. minas rebooted at 13:02 and **both**
`immich-postgres14` and `nextcloud-db` were killed mid-flight — their last
checkpoints are four seconds apart. Each was left with a stale `postmaster.pid` and
`Database cluster state: in production`, so the `verify-pgdata` gate refused to start
them, correctly, on every retry.

The result was `immich.saldivar.io` and `drive.saldivar.io` returning **502 for about
ten hours**, and nothing said so.

✅ Recovered 2026-08-18 — WAL replayed in each database's own image, clean shutdown,
gate passed, 26/26 baseline rows green. Procedure:
[`docs/runbooks/minas-tirith/postgres-unclean-shutdown.md`](docs/runbooks/minas-tirith/postgres-unclean-shutdown.md).

⛔ **The gate is right and must not be loosened.** It cannot distinguish a stale pid
from a second postmaster on the same PGDATA in another namespace, and two writers
lose the database while a refusal is always recoverable.

Two real gaps this exposed:

- **No graceful shutdown ordering.** Nothing stops the kubelet's Pods before the
  pools go away on reboot. Same gap `deploy-rs` (item 9) would otherwise widen, since
  magic rollback reboots.
- ⛔ **Nothing alerts on a 502.** The heartbeat checks Pod readiness, workload counts,
  crash loops and backup freshness — but a database Pod stuck in
  `Init:CrashLoopBackOff` behind a healthy-looking app produced no signal for ten
  hours. `k8s_unhealthy_pods()` excuses `Init:` states as Job-like, and the app Pod
  was `Running`. An ingress-level probe against the recorded baseline is the check
  that would have caught this.

## Not a software problem

The largest remaining risks, recorded because no amount of Nix fixes them.

- **The backup lives on the same machine it protects.** `/storage2` guards against
  corruption, mistakes and bad restores — not fire, theft, or the PSU taking the
  pools with it. There is no off-site copy of ~98 TB.
- **The HBA stays physically installed** (decided 2026-07-30), which makes the
  software gate in the install runbook the primary fence rather than a backup to a
  physical one.
- **No UPS**, and the root NVMe has no power-loss protection. That combination is
  what destroyed the previous btrfs root.
- ⚠️ **One pool member has a damage history: serial `9JH1LNDT`**, a WD Ultrastar
  DC HC530 14 TB, `wwn-0x5000cca258ced1ea`, first member of `raidz2-0`.

  | counter | 2026-08-16 | 2026-08-17 |
  |---|---|---|
  | Current_Pending_Sector | **24** | **0** |
  | Reallocated_Sector_Ct | 41 | 41 |
  | Offline_Uncorrectable | 12 | 12 |

  The pending count cleared to zero across a reboot and resilver **without
  reallocations increasing**, which means those sectors were successfully rewritten
  rather than retired — the disk was healthier than 24 pending suggested. The pool
  reports no known data errors, and the permanent error `storage:<0x41080>` seen on
  2026-08-16 is gone.

  ⛔ **This is not a clean bill of health, and the scrub that would prove one is
  still running** (restarted 2026-08-17 13:55 after the reboot; ~14 h remaining).
  41 reallocated and 12 offline-uncorrectable are chronic, and they did not move.
  Replace it on the next convenient window rather than as an emergency — and
  re-read the counters when the scrub finishes, since an incomplete scrub reporting
  "no known data errors" has simply not reached the bad block yet.

  ⛔ **Identify it by SERIAL or `wwn`, never by `/dev/sdX`.** It was `/dev/sdf` on
  2026-08-16 and is `/dev/sda` after the 2026-08-17 reboot. Device letters are
  assigned in discovery order and move; `smartd` alerts name them, so an alert
  naming a letter must be resolved to a serial before anyone acts on it.

---

## Explicitly rejected

- **`lib/try-import.nix`-style optional imports** — silently make files optional,
  including hardware and disko. Wrong for a repo whose posture is fail-closed.
- **A global overlay layer and `packages/`** — empty machinery until this repo owns
  a custom derivation.
- **Broad nixpkgs-unstable** — pull individual packages when a named one needs it.
- **Machine-class enums** — a class *enum* forces mutually-exclusive naming onto
  capabilities that are not exclusive. Capability and role modules compose; classes
  do not. See `modules/README.md`.
