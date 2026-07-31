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

`modules/nixos/common.nix` exists but **only `nardol` imports it**. `minas-tirith`
reimplements nix settings, GC, timezone, `allowUnfree` and sshd in its own
`system.nix`. Two hosts, two copies, already drifting.

The fix is to move `common.nix` into `mkHost.baseModules` so every host receives it,
with `mkDefault` where hosts legitimately differ.

### Method — the proof is the point, not the edit

1. **Snapshot before.** Dump option values for both hosts to JSON:
   `nix.settings`, `nix.gc`, `nixpkgs.config`, `time.timeZone`, `i18n.defaultLocale`,
   `services.openssh.settings`, `users.users.*.shell`, `security.sudo.*`.
2. Add `common.nix` to `baseModules`.
3. Delete the now-duplicated blocks from `minas-tirith/system.nix`. Where both set
   the same option with different values, `mkDefault` in `common.nix`.
4. **Snapshot after, and diff.** Every single difference must be one you can name
   and justify out loud.

Expected intentional changes for minas-tirith, and nothing else:

- gains the **cuda-maintainers substituters** — genuinely wanted, it is the GPU
  inference host and currently rebuilds CUDA closures from source
- gains `i18n.defaultLocale`

Anything else in that diff is a bug you just introduced. This diff-based proof is
the entire reason this is safe to do later and was not safe to do before the
migration: a surprise here would otherwise have landed on the one install that
cannot be retried.

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

- **Recipient-scoped sops rules** — the current one-admin/one-host rule is exactly
  right for a single secrets file. Extend when `nardol` gets its own secret.
- **Renovate** for input-update PRs — only after CI, or it produces PRs nothing
  validates.
- **Composable capability modules** (`base`, `docker-host`, `gpu-host`) — when hosts
  multiply. Note: **not** machine-classes. A class *enum* forces mutually-exclusive
  naming onto capabilities that are not exclusive; the repo this idea came from
  already violates its own taxonomy (a `local-vm` host that manually imports
  `server`, a `pc` that imports `local-vm`).
- **Reviving the legacy hosts** (`osgiliath`, `pelargir`, …) — one at a time, porting
  to current nixpkgs, rather than dragging eight broken hosts forward.

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
