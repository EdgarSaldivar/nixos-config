# Session handoff — nardol two-player Wolf, portable LUKS unlock, fleet restructure

Written 2026-08-16. Everything below was verified at the time of writing, not recalled.

## Repo state

```
origin/master       3b3c1bc  PinCollector k3s phase one (PRs #2-#7)
master              c78a08a  = origin/master + Wolf/unlock work, merged   [6 ahead, NOT PUSHED]
fleet-restructure   09bed84  = master + the 2026-08-16 cleanup, 18 commits [NOT PUSHED]
```

`master` is ahead of `origin/master` by six commits that have never been pushed. Decide
whether to push or PR them before building anything else on top.

Stale branches were pruned on 2026-08-16: 18 merged `goal-*`, `run-*` and
`feat/*` branches deleted. `legacy/24.11` is kept as the archive marker for the
deleted VM hosts.

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

## Fleet restructure — DONE 2026-08-16

All five agreed items are complete, plus more. See `git log 47bd84c..` for the
commits; each carries its own evidence. Summary:

- documentation drift ✅ — 107k words to 37k, 46 code-to-doc citations repointed,
  the `dd` offset bug fixed, and drift is now a build failure via `docs-contract`
- contracts out of `flake.nix` ✅ — 730 lines to 133, one file per invariant in
  `checks/`, with `scripts/mutation-test.sh` proving each still throws
- evaluation CI ✅ — genuinely two-platform, with a parity gate. ⚠️ The first
  attempt exposed checks for darwin only while the workflow asked for Linux; it
  was fixed in 09bed84. Verify `nix eval .#checks.x86_64-linux` before trusting it
- the four legacy `-vm` hosts ✅ deleted, along with the modules and scripts they
  orphaned
- the three misleadingly-generic modules ✅ moved to `modules/nixos/roles/`, with
  `modules/README.md` recording the placement rule
- `minas-tirith/system.nix` ✅ split into base / networking / hardware-health /
  backup-root-data. Provable no-op: rendered programs byte-identical, all five
  closures unchanged, 24 top-level option definitions before and after.

⛔ **NOT done, deliberately: extracting the embedded shell into
writeShellApplication.** The characterization fixtures land first
(`hosts/nixos/minas-tirith/scripts/tests/`, 10 cases, mutation-proven against the
historical MCE bug) — that was the whole point of doing them before the move. The
extraction itself cannot be gated the way the rest of this work was: every command
is an interpolated store path, so the rendered text necessarily changes and
"byte-identical" stops being available as evidence. It wants its own session with
a supervised run and a rehearsed rollback.

Fixture coverage is 3 of the 6 cases ROADMAP names. Still uncovered: the SQLite
MCE severity filter, dump promotion, and the six backup-health states.

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
