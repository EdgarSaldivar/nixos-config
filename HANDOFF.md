# Session handoff — nardol two-player Wolf, portable LUKS unlock, fleet restructure

Written 2026-08-16. Everything below was verified at the time of writing, not recalled.

## Repo state

```
origin/master       3b3c1bc  PinCollector k3s phase one (PRs #2-#7)
master              c78a08a  = origin/master + Wolf/unlock work, merged   [6 ahead, NOT PUSHED]
fleet-restructure   c78a08a  branched off master, no work yet
```

`master` is ahead of `origin/master` by six commits that have never been pushed. Decide
whether to push or PR them before building anything else on top.

Stale branches worth pruning: ~20 `goal-*` and `run-*` branches, several created this
session (`run-1786650087`, `run-1786650089` hold the Wolf work now merged into master).

## What is DONE and DEPLOYED on nardol

Running generation `n7hbr0mgjclh6ghx20ckh862q65pyxwf`, `docker-wolf` active, 3 LUKS
keyslots on each volume.

**Two-profile Wolf streaming.** A `guest` profile (display name `Guest`) alongside the
existing `user` profile (display name `Edgar`) with a fully private library. No writable
mount is shared between profiles; the only shared bind is the read-only NVIDIA allocator.
- `Edgar` = id `user` -> `/srv/games/steamapps`, `/srv/games/nonsteam`, `/srv/mods`
- `Guest` = id `guest` -> `/srv/games/guest-steamapps`, `/srv/games/guest-nonsteam`,
  `/srv/mods-guest`
- The `user`/"Edgar" id-vs-name mismatch is DELIBERATE. Changing the id would orphan a
  live 4.9G Steam home at `profile-data/user/WolfSteam`.
- The sed/awk config reconciliation was replaced by a transactional `tomlkit` reconciler
  (`hosts/nixos/nardol/wolf-reconcile.py`, 57 tests via
  `nix build .#checks.aarch64-darwin.wolf-reconciler`).

**Portable LUKS unlock.** Deployed but NOT yet exercised by a cold boot.
- USB keyslot 2 enrolled on BOTH volumes, verified to open both.
- Stick: `/dev/disk/by-id/usb-General_USB_Flash_Disk_0305500000000280-0:0`, raw (no
  partition table), 4096-byte key at offset 4194304.
- initrd now takes a DHCP address only — no gateway, no routes, no DNS, no RA.
- initrd SSH is key-only, source widened to RFC1918, forced password-agent command kept.
- Header backups: `~/Nardol-LUKS-Headers-20260815/` on the Mac (pre- and post-slot-2).

## What is NOT done

1. **Cold boot has never happened.** The initrd changes are inert until nardol reboots.
   Do this AT THE MACHINE — if the new initrd misbehaves, recovery is the console.
   Expected: ~10s pause (keyFileTimeout) then Clevis unlocks, because the stick holds a
   valid key but Tang is reachable at home.
2. **`keyFileTimeout = 10` is a guess.** Real cold-boot enumeration measured 1.06s on one
   port, hot-plugged. Measure across all ports at cold boot, then set the final value and
   assert it.
3. **Slot 0 (passphrase) is UNVERIFIED.** Slots 1 and 2 were proven; slot 0 needs
   `cryptsetup open --test-passphrase --key-slot 0` on both volumes. It is the fallback
   everything else depends on.
4. **The nine unlock drills** in `INSTALL-RUNBOOK.md` §13 are unrun.
5. **No sealed offline copy of the USB key.** `/root/nardol.key` was shredded; the stick
   is the only copy. If it dies, slot 2 is gone (slot 0 and Clevis remain).

## In-flight: fleet restructure

Scope agreed fleet-wide, invariant agreed as provable-no-op.

**The closure harness** — `scratchpad/closure-equiv.sh`, MUST be copied somewhere durable.
Every NixOS host embeds the git revision via `system.configurationRevision`
(`lib/mkHost.nix:13-17`), so any commit changes `toplevel.drvPath` regardless of
behaviour. The harness pins that stamp via `extendModules` so a hash difference means a
REAL difference. Verified in both directions: a comment + renamed `let` binding leaves
hashes untouched; `keyFileTimeout` 10 -> 11 moves them.

Reference values at `c78a08a`, pinned-revision — every step must leave these unchanged:
```
nardol        xa9m6923q3q91ch7g3jgdalvf2v90lb0
minas-tirith  2si4niz7jvxg1kja889h03bfb9yyh6rp
osgiliath     xisvrbbdd1gqdw0r5aqf8d10dxh79q2v
pelargir      8jvnlz9lsfgbki99vnzcla221682f5ph
dol-amroth    r93nh5xbhjryfnjrw3dqpqkniwgkpqbs   (no revision stamp; darwin skips mkNixos)
```

**Agreed work, in value order (revised after codex's independent review):**
1. **Documentation drift** — hash-neutral, independently useful. Known-wrong today:
   - `README.md:91-103` claims three checks, all eval-only. There are FOURTEEN and Wolf
     runs pytest, so the `--no-build` command at `README.md:41-47` does not run what is
     advertised.
   - `flake.nix:131` still says "Both checks".
   - `pelargir/secrets.nix:453-456` says value-only rotations need a manual restart,
     contradicting automatic `restartUnits` at `:193-196`, `:277`, `:401` and the
     enforced contract at `flake.nix:256-290`.
   - `INSTALL-RUNBOOK.md` §10 has a `dd` OFFSET BUG: it says `seek=1`/`skip=1`, which is
     byte 4096, not 4 MiB. Correct is `seek=1024`/`skip=1024`. This bug was made and
     caught live — the read-back "verified" at the same wrong offset.
   - `nixos-rebuild --rollback` does NOT work here (no channel). The working method is
     `nix-env --switch-generation N -p /nix/var/nix/profiles/system` then that
     generation's `switch-to-configuration switch`. Not documented anywhere.
2. **Extract the ~600 lines of contracts out of `flake.nix`** (735 lines, 14 checks,
   host construction ends at :124) AND add evaluation CI in the same change. Critical:
   **the harness is BLIND to this** — deleting every check leaves all five hashes
   unchanged. There is no evaluation CI today (`ROADMAP.md:61-85`). Expose the suite as
   `checks.x86_64-linux` with per-system `checkPkgs`, not Darwin-only `devPkgs`.
3. **Delete the four legacy `-vm` hosts** — `hosts/nixos/{builder,minas-tirith,osgiliath,
   pelargir}-vm`, ~84K total, unwired at `flake.nix:107-111`, referenced only there and
   in `README.md`. User confirmed they are no longer wanted.
4. **Rename the three misleadingly-generic modules** (lowest value). They are
   nardol-specific but named fleet-generic, and only nardol imports them
   (`hosts/nixos/nardol/default.nix:16-19`). Agreed approach is a role directory, NOT
   parameterisation, because the user may add a second gaming host and its requirements
   are unknown:
   - `modules/nixos/docker.nix` -> `modules/nixos/roles/game-streaming-docker.nix`
   - `modules/nixos/gaming.nix` -> `modules/nixos/roles/game-streaming.nix`
   - `modules/nixos/nvidia.nix` -> `modules/nixos/roles/nvidia-headless.nix`
   Future split points are documented in the codex consult: `/srv/docker` and its
   `RequiresMountsFor` are host-specific; Wolf device/seat9 rules are Wolf-specific;
   `open = true` assumes the RTX 4090 generation.
5. **Split `minas-tirith/system.nix`** (1342 lines; backup program at :233-1280, timer
   and failure handling to :1322). Codex's warning: do NOT extract this or
   `monitoring.nix:50-604` for aesthetics first. ~1,500 lines of safety-critical shell
   have almost no behavioural test surface; add characterization tests for mount/dataset
   refusal, failure-marker persistence, retention failure and heartbeat interpretation
   BEFORE moving anything.

**Explicitly leave alone:** the Pi builder/package-set divergence (`flake.nix:92-105`,
`lib/mkHost.nix:25-40`), the long incident comments, the deliberate duplication between
runtime policy and independent contracts, and pelargir's frozen manifest filenames.

## Working practice for this repo

- **Consult AND cross-review with codex** on each item — the user asked for both.
  `codex-seat run --kind research|audit|planning --cd <worktree> "$SPEC"`.
- The terminal review gate takes ~15 min, longer than `supervise` waits, so `supervise`
  records a "block" and hits its breaker at 3. That is NOT a quality verdict — read the
  gate's actual output before concluding anything.
- Seat worktrees are created from committed HEAD, so a seat cannot see uncommitted work,
  and cannot read spec files under `/private/tmp` (sandbox denies it — pass specs inline).
- Seats cannot run `nix`. The controller stages and gates.

## Hard-won gotchas

- **Deploying a branch that lacks work already on the machine breaks it.** Deploying the
  master-based unlock branch reverted `wolf.nix` while the `guest` config it had written
  stayed on disk; the old validator rejected it and Wolf stayed down ~4 minutes. Branches
  are independent in FILES but not in LIVE MACHINE STATE.
- **Verifying a write by reading it back through the same arithmetic proves nothing.**
  The `dd` offset bug passed its own read-back. What caught it was testing through the
  consumer's path (`cryptsetup --test-passphrase`).
- **Wolf's HTTP accept loop can wedge** — port 47989 showed `Recv-Q=61` unaccepted
  connections while HTTPS/RTSP were fine and the unix API answered. Moonlight reports the
  host as unreachable. Restarting `docker-wolf` clears it; cause unknown, watch for it.
- **Docker creates missing bind-mount PARENTS as root:root 0755** before container init
  runs. That is why Guest's first Steam launch died with
  `mkdir: cannot create directory '/home/retro/.steam/ubuntu12_32': Permission denied`.
  Fixed by `chown -R 1000:1000` on the profile home; a declarative tmpfiles fix is still
  OUTSTANDING and will bite any third profile.
- **Wolf UI hardcodes `MultiUser = false`** when launching an app normally. The Co-op
  button (`App.cs:422 OnCoopPressed`) is the multi-user path, and it is DISABLED once the
  app is already running — press it before launching, not after.
- The GStreamer `not negotiated` warnings at 2560x1440 are BENIGN — a capsfilter
  negotiating before the app container produces its first frame. Not a defect.
