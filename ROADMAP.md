# Roadmap

Open work only. Anything finished is deleted from this file and lives in git
history — a backlog that lists completed items stops being read.

Last audited **2026-08-16** against the source, not against recollection. Items
marked ⏳ cannot be settled from this repository; each names the exact observation
that would settle it, because "we think it works" is how the last set of stale
entries happened.

---

## 1. Move the cluster off `/home/edgar/git/docker`

The docker→k3s migration is complete and docker runs zero containers, but the old
Compose repository's *directory tree* was never migrated.

✅ **immich and shelfmark are DONE** — relocated to `/usr/local/etc/<service>/config`
and deployed 2026-08-16. Procedure and evidence in
[`docs/runbooks/minas-tirith/relocate-immich-shelfmark.md`](docs/runbooks/minas-tirith/relocate-immich-shelfmark.md).

**Two bindings remain** — both halves of the traefik file provider, which must move
together. Procedure prepared in
[`docs/runbooks/minas-tirith/relocate-traefik-file-provider.md`](docs/runbooks/minas-tirith/relocate-traefik-file-provider.md).
✅ The prerequisite is DONE: `traefik.yml` has been removed (item 11), so the
file-provider directory now contains **only generated files** — no unmanaged file
to carry across and no plaintext credential to migrate.
`checks/external-checkout-dependency.nix` pins the
exact set — a sixth fails the build, and removing one fails the build until its
entry is deleted, so progress stays visible instead of being discovered by grep.

| what | if the directory goes away |
|---|---|
| traefik's file provider — the 22 generated route files, `type: Directory` | Pod will not schedule; **all 26 public hostnames down** |
| `manifests/traefik.yaml` mounts that same directory | two halves of one dependency; they go together |
| immich config, `type: Directory` | Pod will not schedule |
| shelfmark config, `type: Directory` | Pod will not schedule |
| shelfmark `users.db`, a declared backup dump target in `backup-root-data.nix` | a live database, in the backup set — its declaration moves with the config |

✅ The cheap half is done: minas activation now **fails** rather than warning when
that directory is missing. Previously a rebuild on a replacement host could report
success while installing zero ingress routes.

What remains is relocation, in this order — immich and shelfmark are independent
and easy; traefik touches the public ingress path for 26 hostnames and wants its
own window with a rehearsed rollback. When the check's list reaches zero, delete
the check and the directory in the same change.

## 2. Extract the embedded shell

`minas-tirith/backup-root-data.nix` carries a ~1,030-line backup program and
`monitoring.nix` a ~555-line heartbeat, both inside Nix string literals. The
*behaviour* is proportionate to the risk; the *packaging* is invisible to
ShellCheck and cannot be exercised against fakes without evaluating Nix first.

✅ **The characterization fixtures are DONE** — `hosts/nixos/minas-tirith/scripts/tests/`,
22 cases covering all six originally-named fixtures, run by the
`minas-shell-fixtures` check against the RENDERED programs so they cannot drift
from what is deployed. Mutation-proven: reintroducing the historical MCE
section-scoping bug fails three fixtures, and changing the promotion size floor
fails the anti-drift gate.

What remains is the extraction itself, and it needs its own session because it
**cannot be gated the way the rest of this repository's refactors were**:

- every command in the program is an interpolated store path, so moving to a real
  `.sh` requires `substituteAll` placeholders or bare names on `PATH` — the
  rendered text necessarily changes and byte-equality stops being available as
  evidence
- `writeShellApplication` adds its own shebang, strict-mode flags and a
  `runtimeInputs` PATH, while the heartbeat currently relies on
  `systemd.services.path` as its **only** PATH, so command resolution can change
  silently
- 22 fixtures are a real net over ~1,600 lines, not an equivalence proof

Gate it on the full rendered unit — ExecStart, environment, PATH, credentials —
not on body text, and run it supervised with a rehearsed rollback.

## 3. The heartbeat has been red for a week, and two of the four reasons are false

Found 2026-08-16 by tracing an actual heartbeat run. **The alerting works
correctly** — it POSTs `UNHEALTHY: …` to `/fail` with the full problem list. The
problem is signal quality: two false positives are drowning two real signals, and
nothing has been acknowledged since 2026-08-09.

| reported | reality |
|---|---|
| `zpool: pool: storage` | **REAL.** One corrupted file — a single TV episode. Pool healthy: 7/7 ONLINE, 0/0/0 errors, raidz2 intact. Delete/re-download it, then `zpool clear storage`. |
| `SCRUTINY DISK ALERT … nvme0` | **REAL, watch.** SMART PASSED, spare 96%, critical warning 0x00 — but **27 media/data integrity errors, up from 24**, on the root NVMe at 33% wear with 153 unsafe shutdowns and no power-loss protection. Latched since 2026-08-11 pending *explicit* acknowledgement, by design: `sudo rm /var/lib/scrutiny/alert.latched`. |
| `k8s pods NOT Ready` ×3 | **FALSE.** All three are `Completed` Jobs (jellyfin-quiesce ×2, pin-collector-migrate). The check counts a finished Job pod as unhealthy. |
| `DEGRADED dumps` ×3 | **FALSE.** Docker-era artifacts belonging to *stopped rollback containers*. The live k3s dumps are fresh. |

### The dump false positive, and the fix that does NOT work

`backup-root-data.nix` walks `docker ps -a` — including **stopped** containers —
and degrades if their artifact is stale. `immich-postgres14`, `nextcloud-db` and
`infra-postgres-1` are `exited`, retained deliberately as the pre-migration
rollback. Their docker-era artifacts are correctly frozen at 2026-08-09, while the
same databases are dumped fresh under `k8s-<ns>-<container>` names.

⛔ The obvious fix — "if a fresh `k8s-*-<name>` artifact exists, don't degrade" —
was consulted on and **rejected**:

- it does not clear `infra-postgres-1`, which is *parked* with no k3s successor,
  so the marker stays red and the problem is not solved;
- `k8s-*-$c.sql.gz` can suffix-match a different, longer container name;
- it misses the `.dump` and `.sql.gz.age` artifact forms;
- ⛔ the program runs under `set -euo pipefail`, so a bare `stat`, unmatched glob
  or failing substitution inside the added conditional can **abort the unit before
  the k3s, SQLite, rsync, snapshot and stamp stages run**. A check that looks
  read-only can kill the backup.

✅ **Do instead:** an explicit retired/parked container list naming exactly
`immich-postgres14`, `nextcloud-db` and `infra-postgres-1`, with a removal date.
Unknown stopped Postgres containers must keep degrading — that crash-detection
guarantee is deliberate. Add fixtures: each retired name ignored; an unknown
exited Postgres still degrades; live k3s successors still guarded by their exact
never-created and freshness expectations.

### Two related bugs found in the same consult

- ⛔ **Tracearr's dump age is never checked.** The freshness walks cover
  `.sql.gz` and `.sql.gz.age`; tracearr deliberately produces `k8s-media-tracearr.dump`
  because its `.sql.gz` form is **not restorable**. Its existence is accepted but
  its staleness is invisible.
- **The never-created check accepts any of three extensions** for every database
  rather than the exact expected format, so an obsolete wrong-format artifact can
  satisfy existence. Worst precisely for tracearr.

Both want their own reviewed change, not bundling into the alarm fix.

### And the general shape of it

Artifact names are derived from source paths or container names, and **nothing
prunes an artifact when that name changes at migration**. This bit shelfmark today
(its relocation created a new dump name; the old one had to be deleted by hand)
and is the root cause of the docker false positive. Orphan retirement needs an
explicit policy rather than name inference.

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

Held until roughly **2026-09-09** (30 days after the last container stopped) so a
rollback target remains. `containers.nix` still enables the daemon.

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
