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

⚠️ **Four stale dump artifacts, one of which needs a decision.**

| artifact | age | |
|---|---|---|
| `nextcloud-db.sql.gz` | 7d | superseded by a fresh `k8s-nextcloud-nextcloud-db.sql.gz` |
| `immich-postgres14.sql.gz` | 7d | superseded by a fresh `k8s-immich-immich-postgres14.sql.gz` |
| `_usr_local_etc_jellyfin_config_data_data_library.db` | 8d | superseded by the fresh staging-path capture |
| **`infra-postgres-1.sql.gz`** | 10d | ⛔ **no k3s successor.** This 16 KB file is the only remaining copy of that database. Do not delete it as "an orphan" without deciding the data is not wanted. |

Nothing flags these now that the containers are gone, so they will sit forever. That
is the general problem below.

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

## 5. Stale k3s manifests are never pruned

Removing a manifest from the repository does not remove the applied resource.
`manifests.nix` deliberately only *reports* a stale auto-deploy file and requires a
manual deletion, because deleting automatically would drop a live route mid-
activation. That trade is sound; the gap is that nothing tracks what has
accumulated. A periodic reconciliation report would close it.

## 6. Validate manifest SCHEMAS

✅ Object-identity uniqueness is done — `checks/manifest-objects.nix` asserts no
two manifests declare the same `(apiVersion, kind, namespace, name)`, which is the
collision that makes two k3s AddOns fight over one object. 130 objects across 45
manifests.

What remains is schema validation: a pinned `kubeconform` pass to catch a manifest
that is valid YAML and invalid Kubernetes. It needs a schema bundle vendored into
the store (the check sandbox has no network) plus explicit schemas for the CRDs
this cluster installs — traefik's IngressRoute/Middleware, cert-manager's
Issuer/Certificate, and the sealed nothing else. Also still unchecked:
immutable-field changes, which only the API server can adjudicate.

## 7. Collapse the Cloudflare list to one source

✅ Divergence is now impossible — `checks/cloudflare-ranges.nix` asserts the
nftables forward rules and the traefik ipAllowList declare the same 22 ranges,
v4 and v6 compared separately.

The duplication itself remains. Rendering the manifest from the Nix list changes
how that manifest is delivered, into an auto-deploy directory with a frozen
basename feeding the live ingress, so it wants a window. Lower priority now that
the risk it carried is gated.

## 8. sops has a single human recipient

`.sops.yaml` declares exactly one admin identity. Every host key is recoverable by
reinstalling that host; the human key is not. Add a second trusted human recipient
before it becomes an operational dependency, then `sops updatekeys` each file.

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

## 11. `dungeon.saldivar.io` — routing retired, DNS still yours

✅ **Done at the traefik layer, 2026-08-16.** Its four routers and three services
pointed at `192.168.6.94`, a dead host, and returned `000`. They lived in a
hand-maintained `traefik.yml` inside the file-provider directory — which is now
**deleted**, taking a plaintext bcrypt credential out of that directory with it.

Verified before and after: the full external ingress acceptance from pelargir
passes against the recorded baseline, and `traefik.saldivar.io` still redirects to
authentik. The dashboard was never served by that file — the generated
`k8s-authentik-gate.yml` routes it to `api@internal` at priority 9000, shadowing
the hand-maintained router entirely.

Rollback: `/var/tmp/traefik.yml.disabled` and a timestamped backup beside it.

⏳ **Still open, and yours:** the DNS record for `dungeon.saldivar.io` still
exists, so the hostname resolves and fails at TLS, which is why the acceptance
baseline still lists it as an expected `000ERR`. Retire the DNS record, then
remove that row from
`hosts/nixos/minas-tirith/baselines/minas-ingress-authentik-baseline-*.txt`.

## 12. Decommission docker on minas

✅ **The 31 stopped containers are GONE — removed 2026-08-16.** With them went the
last plaintext copy of credentials that now live properly in sops: `docker inspect`
across the set had yielded **14 credential-shaped environment variables**, baked
into container metadata at create time and untouched by deleting the Compose tree.
That count is now **0**.

Removed with `docker rm` and no `-v`, so nothing that holds data was touched:
**named volumes 59 → 59, images 51 → 51**. Verified after: both nodes Ready, every
Pod Running, ingress matching baseline.

The rollback the 2026-09-09 window was protecting is therefore deliberately spent.
It was already largely notional — the Compose tree had been deleted, so the four
containers that bind-mounted it could not start at all, and any of the remaining 27
that duplicated a live k3s workload would have been a second writer against data
the cluster owns (for traefik, two writers on `acme.json`, which is how the
wildcard certificate gets lost).

⏳ **What is left of this item:** `containers.nix` still enables the daemon, and
**~79 GB of images** remain, now 100% unreferenced. Removing the daemon is the
actual decommission; reclaiming the images is a one-liner whenever the disk is
wanted. 57 local volumes (3.4 GB) also survive — they are now unreferenced too, but
they are the only remaining copy of anything the old stack wrote, so treat them as
a separate, deliberate decision rather than folding them into a prune.

✅ The docker-only resource sampler is gone (it had been waking every five minutes
to sample an empty set). ⚠️ Its collected series remains at
`/var/lib/resource-samples/samples.csv` on minas, is NOT in the backup source
set, and is the empirical basis for every resource request in the manifests.
Preserve it deliberately or discard it deliberately.

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
