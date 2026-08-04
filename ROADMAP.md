# Post-migration roadmap

Work deliberately deferred until `minas-tirith` is migrated to NixOS and observed
stable. Written 2026-07-30, immediately before that migration, while the reasoning
is fresh — the *why* matters more than the task list, because the reasons are what
decide whether an item is still worth doing when you get to it.

**Precondition for everything below:** minas-tirith installed, all 39 containers
restored and functionally verified (not merely "running"), one successful nightly
backup with database dumps present, and the heartbeat proven end-to-end. Until then
the only sane change to this repo is one that fixes something broken.

---

## 1. CI — do this first

**Effort:** ~1 hour **Risk:** none **Blocks:** items 2 and 3

**Status:** open — there is still no Nix evaluation CI.

GitHub Actions on push/PR:

```
nix flake check          # the invariants
nix eval …toplevel.drvPath   # per host, all three
nixfmt --check           # formatting gate
```

### Fix this first, it is a bug in the current setup

`checks` is currently scoped to `aarch64-darwin` only (`flake.nix`, `devSystem`).
Free CI runners are Linux, so **CI would silently skip every check** and report
green. The checks are pure evaluation and work on any system — expose
`checks.x86_64-linux` as well before wiring CI, or the whole exercise is theatre.

**Why it comes first:** the `minas-tirith-disko-targets` check is the thing standing
between a careless edit and nine live pool members. Right now it runs when someone
remembers. In CI it runs on every push — and it should be green *before* anyone
starts refactoring, so a later break is unambiguous.

---

## 2. Always-applied base modules

**Effort:** ~half a day **Risk:** moderate — hence the method below

**Status:** done 2026-08-04. `common.nix` is now in `mkHost.baseModules`, the
identical host-local settings were removed, and host-specific settings remain
explicit. `system.configurationRevision` also records the clean or dirty flake
revision in every NixOS deployment.

Before this change, only `nardol` imported `modules/nixos/common.nix` while
`minas-tirith` and pelargir repeated pieces of the same policy. The baseline now
enters through `mkHost.baseModules`; identical host-local definitions were removed,
while minas-tirith's distinct `auto-optimise-store` behavior and each host's SSH
details remain explicit. Intentional baseline additions include the shared package
set, bounded store maintenance, locale, and cache policy.

The controller's before/after evaluated-config comparison remains the proof for
this refactor: every difference must be attributable either to removing an
identical duplicate or to applying the documented fleet baseline.

**Do not** replace `mkHost` with a B-style universal assembler. It is a good, small
abstraction. Improve it in place: apply the baseline automatically, and require
`hostname`/`system` explicitly rather than defaulting.

---

## 3. Extract the embedded scripts

**Effort:** ~half a day **Risk:** low **Payoff:** the highest of these four

`monitoring.nix` carries a ~400-line shell script inside a Nix string literal, and
`system.nix` is ~540 lines with the backup logic embedded the same way. The
*behaviour* is proportionate; the *packaging* is untestable.

Move them to real `scripts/*.sh` referenced via `writeShellApplication`, then make
the ad-hoc fixture tests permanent. During the 2026-07-30 audit rounds each of these
was verified by hand in throwaway files and then thrown away:

| Fixture | What it catches |
|---|---|
| MCE severity filter | corrected-vs-uncorrected; a corrected-only DB must count 0 |
| MCE section parser | must count 2 on a mixed log, not 6 |
| SMART drift awk | RAW_VALUE column, ATA and NVMe forms; 24→27 must alert |
| Backup promotion | empty-but-exit-0 dump must NOT overwrite a good one |
| Snapshot rotation | 20 dailies keep=14 prunes exactly the 6 oldest |
| Backup health | 6 states incl. missing stamp = UNHEALTHY |

The MCE regex was wrong through **three** consecutive audit rounds — first it missed
every error, then it counted every error class, then it counted corrected errors.
That is precisely the kind of bug a fixture catches instantly and code review does
not.

---

## 4. deploy-rs

**Effort:** ~2 hours **Risk:** low **When:** once you deploy to minas-tirith remotely

Magic rollback: activate, and if the host does not confirm connectivity within a
timeout, it **reverts automatically**. For a machine an hour away, where a bad
`nixos-rebuild switch` means a drive, that is the single most valuable piece of
tooling available.

Colmena is fleet machinery; three hosts do not need it.

Do **not** copy the friend's `rsync-switch.sh` pattern — it has no rollback protocol,
which is the one property that matters here.

---

## Deferred further, with reasons

- **Recipient-scoped sops rules** — the current single human recipient is a known
  continuity gap. Add another trusted human recipient before it becomes an
  operational dependency, and extend host recipients when `nardol` gets secrets.
- **Renovate** for input-update PRs — only after CI, or it produces PRs nothing
  validates.
- **Composable capability modules** (`base`, `docker-host`, `gpu-host`) — in
  progress: `fleet.k3sNode` is the first extracted capability. Continue as hosts
  multiply. Note: **not** machine-classes. A class *enum* forces mutually-exclusive
  naming onto capabilities that are not exclusive; the repo this idea came from
  already violates its own taxonomy (a `local-vm` host that manually imports
  `server`, a `pc` that imports `local-vm`).
- **Reviving the legacy hosts** (`osgiliath`, …) — in progress: pelargir is done.
  Continue one at a time, porting to current nixpkgs rather than dragging every
  broken host forward.

## Tracked operational gaps

- No Nix evaluation CI; item 1 remains the first tooling priority.
- The k3s auto-deploy directory has no stale-manifest cleanup, so removing a
  manifest from the repository does not remove the previously applied resource.
- sops depends on a single human recipient, creating a continuity risk.
- Cloudflare CIDR lists are duplicated between `wireguard.nix` and
  `manifests/ingress.yaml` and must be kept in sync manually.

## Explicitly rejected

- B's `lib/try-import.nix` — silently makes files optional, including hardware and
  disko. Wrong for a repo whose posture is fail-closed.
- B's global overlay layer and `packages/` — empty machinery until this repo owns a
  custom derivation.
- Broad use of nixpkgs-unstable — pull individual packages when a named one needs it.

---

## Not a software problem

Recorded because it is the largest remaining risk and no amount of Nix fixes it:

- **The backup lives on the same machine it protects.** `/storage2` guards against
  corruption, mistakes and bad restores — not against fire, theft, or the PSU taking
  the pools with it. There is no off-site copy of ~98 TB.
- **The HBA stays physically installed** (decided 2026-07-30). That makes the
  software gate in the install runbook the primary fence rather than a backup to a
  physical one.
- **No UPS**, and the root NVMe has no power-loss protection. That combination is
  what destroyed the previous btrfs root.
