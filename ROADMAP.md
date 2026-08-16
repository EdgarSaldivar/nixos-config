# Roadmap

Open work only. Anything finished is deleted from this file and lives in git
history — a backlog that lists completed items stops being read.

Last audited **2026-08-16** against the source, not against recollection. Items
marked ⏳ cannot be settled from this repository; each names the exact observation
that would settle it, because "we think it works" is how the last set of stale
entries happened.

---

## 1. Move the cluster off `/home/edgar/git/docker`

**The largest structural problem in the fleet.** The docker→k3s migration is
complete and docker runs zero containers, but the old Compose repository's
*directory tree* was never migrated. It is now the on-disk config store for three
live workloads and the drop point for the public ingress routes:

**Five bindings remain**, and `checks/external-checkout-dependency.nix` pins the
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

## 3. Decide the osgiliath staging question

Osgiliath is **not deployed** — still on docker/ubuntu. But `pelargir/manifests.nix`
delivers five osgiliath manifests to the live cluster, and four of them declare
`replicas: 1` with `nodeSelector: kubernetes.io/hostname: osgiliath`. With no such
node they are permanently Pending.

Either gate them to zero until the host exists, or accept and document the Pending
state so it does not read as a fault later. Right now it is neither.

## 4. Stale k3s manifests are never pruned

Removing a manifest from the repository does not remove the applied resource.
`manifests.nix` deliberately only *reports* a stale auto-deploy file and requires a
manual deletion, because deleting automatically would drop a live route mid-
activation. That trade is sound; the gap is that nothing tracks what has
accumulated. A periodic reconciliation report would close it.

## 5. Validate manifest SCHEMAS

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

## 6. Split the ingress Cloudflare source of truth

The Cloudflare CIDR list is duplicated between `pelargir/wireguard.nix` and
`pelargir/manifests/ingress.yaml` — 22 entries on each side, currently in
agreement and manually kept so. Render one from the other.

## 7. sops has a single human recipient

`.sops.yaml` declares exactly one admin identity. Every host key is recoverable by
reinstalling that host; the human key is not. Add a second trusted human recipient
before it becomes an operational dependency, then `sops updatekeys` each file.

## 8. deploy-rs

Magic rollback: activate, and if the host does not confirm connectivity within a
timeout it **reverts automatically**. For a machine an hour away, where a bad
switch means a drive, that is the single most valuable piece of tooling available.
Colmena is fleet machinery; four hosts do not need it.

## 9. Renovate

Input-update PRs — now that evaluation CI exists to validate them.

## 10. Retire or restore `dungeon.saldivar.io`

The backend is dead and the hostname is still an expected `000ERR` in the live
ingress baseline. Decide, then make the baseline say so.

## 11. Decommission docker on minas

Held until roughly **2026-09-09** (30 days after the last container stopped) so a
rollback target remains. `containers.nix` still enables the daemon; the resource
sampler in `resource-sampling.nix` still runs every five minutes measuring a
migration source that no longer exists.

## 12. Namespace-level NetworkPolicy

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
