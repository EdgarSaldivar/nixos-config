# Working rules for this repository

For humans and for AI agents alike. Everything here was learned by breaking
something. Read it before you touch a host.

This fleet is small but not forgiving: `minas-tirith` is an hour's drive away and
serves 26 public hostnames, `pelargir` is the **sole** k3s control plane and the
**only** node that delivers manifests, and one machine holds ~98 TB with no
off-site copy.

---

## 1. Deploy rules that have caused real outages

- **`nixos-rebuild` cannot run from the Mac.** `rsync` the tree to the host and run
  the host's native `nixos-rebuild` with an **absolute** flake path. Never `~`
  under `sudo`.
- **Three beats, and the third is the one that gets skipped:**
  `rsync (uncommitted) → build/test on host → commit → RSYNC AGAIN → switch`.
  A switch that redeploys the config the host already had is **indistinguishable
  from a real deploy** in its output.
- **Flakes only see tracked files.** `git add` before building.
- **A commit can span TWO hosts.** `manifests/*` and `pelargir/*` are delivered by
  **pelargir**; `traefik-routes.nix` and the `minas-tirith/*.nix` modules by **minas**. For manifest
  changes rebuild **pelargir first**. Rebuilding minas alone caused the only outage
  of the migration.
- **Adding a namespace and its Secret in one commit makes `nixos-rebuild switch`
  exit 4.** It self-heals on the `k3s-apply-secrets` restart. Verify, don't panic.
- The ssh alias is **`minas`**, not `minas-tirith`. `docker` there needs `sudo` and
  silently reports **zero** containers without it.
- ⛔ **traefik is the live public ingress as a k3s Pod.** Any change to
  `spec.template` in `manifests/traefik.yaml` triggers a rollout, and with
  `strategy: Recreate` on a pinned singleton that is a **full ingress outage**, not
  a rolling update. Run `kubectl diff -f <candidate>` first — **empty output is the
  proof** there is no template change.
- ⛔ **`nixos-rebuild test` on pelargir is NOT a harmless validation.** Activation
  runs the manifest copier, which rewrites every auto-deploy file and makes k3s
  reconcile. There is no dry run for manifest delivery.
- **Rollback here is not `nixos-rebuild --rollback`.** This flake has no channel.
  The working method is:
  ```sh
  sudo nix-env --switch-generation N -p /nix/var/nix/profiles/system
  sudo /nix/var/nix/profiles/system/bin/switch-to-configuration switch
  ```

## 2. Probes that lie on this fleet — each cost hours

- **`ss` is blind to k8s `hostPort`** (CNI PREROUTING DNAT). It reported 8081 free
  while calibre owned it. Cross-check the **iptables NAT table**.
- **`bash </dev/tcp/HOST/PORT` succeeding proves only that SOMETHING answered**,
  never that the intended host did. A router or middlebox completes the handshake.
  This exact false fact sent two sessions down the wrong path.
- **An access log is not a packet trace.** It cannot record a ClientHello that never
  arrived.
- ⛔ **An asymmetric-routing failure leaves NO conntrack entry, increments NO
  iptables counter, and never matches the CNI hostPort DNAT rule** — so every
  first-line tool says "the packet never arrived" while `tcpdump` plainly shows it
  arriving with the correct destination MAC. **When those two disagree, run
  `ip route get <peer>` immediately.** That one command was the answer to the
  2026-08-10 outage, and it was found last rather than first.
- **Verifying a write by reading it back through the same arithmetic proves
  nothing.** A `dd` offset bug passed its own read-back because both used the wrong
  offset. Verify through the *consumer's* path instead — for LUKS, that is
  `cryptsetup open --test-passphrase`.
- ⛔ **kubelet sends the Pod IP as the `Host:` header on an `httpGet` probe.**
  nextcloud answers 400 to that and failed startup **17 times**. Worse than the
  failure itself: a failing `startupProbe` SUPPRESSES readiness and liveness
  entirely, so the Pod is neither restarted nor removed from endpoints — it just sits
  there looking Running. Confirm what the app does with that Host, or set an explicit
  one. (Recorded on traefik's probe in `manifests/traefik.yaml`, where it was
  measured; restored here 2026-08-21 after the docs consolidation dropped it.)
- ⛔ **`psql -c` does NOT expand `:'var'`.** psql substitutes variables only for SQL
  arriving on **stdin or from a file**; with `-c` the literal `:'var'` reaches the
  server, which answers `syntax error at or near ":"`. Verified 2026-08-09.
  ⚠️ It reads like a broken *database* rather than a broken command, and in a scripted
  gate whose result is a count it presents as "no rows matched" rather than an error —
  a known-file capture returned `NOT-IN-FILECACHE` for 20 of 21 rows this way, and the
  row count looked healthy. Feed SQL on stdin (heredoc) whenever you use `-v`.
  ⛔ Neither a grep gate nor `bash -n` catches this: it is shell-valid and
  semantically dead.
- `tcpdump` is not installed on minas; use `nix-shell -p tcpdump --run '...'`.
- minas has **no `openssl`, `jq`, `dig`, or python `cryptography`** — python3
  stdlib only.

## 3. Invariants you must not break

- ⛔ **`hosts/nixos/minas-tirith/disko.nix` is the only file that can destroy the
  pools.** Nine of that host's ten drives are live ZFS members. `nix flake check`
  asserts the destroy list is exactly the one Samsung NVMe and that
  `disko.devices.zpool` is empty. Read the header before any disk work.
- ⛔ **k3s auto-deploy basenames are FROZEN.** k3s does not prune a file that
  disappears, and the AddOn identity is derived from the basename. Renaming an entry
  creates a NEW object set and leaves the old one owning its resources. See
  `hosts/nixos/pelargir/manifests.nix`.
- ⛔ **Durable state belongs in git.** Never `kubectl scale` a workload and leave the
  manifest disagreeing. k3s re-applies a manifest when its file **checksum changes
  OR the server restarts**, so an imperative value survives only until the next edit
  or restart — and then the declared value is silently reasserted. This is also why
  hand-scaling appears to survive an ordinary rebuild: installing byte-identical
  content does not change the checksum.
- **Secrets never enter the Nix store.** sops-nix renders them at runtime; the k3s
  applier reads them from tmpfs. Rotating a *value* re-runs the applier
  automatically via `restartUnits`, and `nix flake check` enforces that wiring.

## 4. Verification gates

Two instruments, and they prove different things. Neither is a substitute for the
other.

```sh
nix flake check            # FULL, not --no-build: two checks actually build+test
bash scripts/closure-equiv.sh .   # ~40s, all five hosts
```

`closure-equiv.sh` pins `system.configurationRevision` so a hash difference means a
**real** difference. Without it, every commit moves `toplevel.drvPath` regardless of
behaviour and the refactor signal drowns in the revision stamp.

**Know what the harness cannot see.** It compares evaluated host closures, so it is
blind to: deleting a check (delete all 24 and every hash is unchanged), docs,
CI, tags and branches, `.gitignore`, manual scripts and images, live sops *values*,
runtime filesystem state, and anything k3s already applied. A matching closure is
**not** proof of a live-fleet no-op.

For a refactor claimed to be a no-op, record the expected delta up front: which
hosts should change, and why. "CE unchanged" is only evidence if you predicted it.

## 5. Working with the Codex seat pool

- Seats run in an isolated worktree created from **committed HEAD** — a seat cannot
  see uncommitted work.
- Seats **cannot run `nix`**. Acceptance criteria must be text/grep checks. The
  controller stages and runs every `nix flake check` and closure comparison.
- Seats cannot read spec files under `/private/tmp` (the sandbox denies it). Pass
  specs inline in the task payload.
- The terminal review gate takes ~15 min, longer than `supervise` waits, so it
  records a "block" and trips its breaker at 3. **That is not a quality verdict** —
  read the gate's actual output before concluding anything.
- Every Codex diff gets a Claude review before it counts as done.

### Write acceptance commands defensively — a broken judge looks exactly like a broken worker

Acceptance runs under zsh. An unquoted glob in a flag argument is expanded by the
shell *before* the command runs, and zsh aborts with `no matches found` when it
matches nothing:

```sh
grep -r --include=*.nix 'pattern' .     # ✗ dies before grep starts
grep -r --include='*.nix' -e 'pattern' . # ✓
```

This cost a full Phase-1 run: three iterations and 501k tokens spent because the
check could never pass regardless of what the worker did. Quote every glob, use
`-e` for patterns that begin with `-` or contain alternation, and satisfy yourself
that a check can actually *fail* for the right reason before shipping it.

Corollary: prefer acceptance commands that are boring. `test -f`, `test ! -d` and
a literal `grep -q` are hard to get wrong. Clever one-liners are how you end up
debugging the judge.

## 6. Documentation contract

- **Source owns facts. Runbooks own actions. ADRs own reasons. Runtime status
  belongs in monitoring — not in Markdown.**
- A runbook says what is *configured*, never what is *running*. "osgiliath runs the
  collector hourly" was wrong for weeks because a doc stated declared config as
  observed fact.
- Plans, reviews, handoffs and ledgers are history. They do not live beside live
  procedure, because a reader cannot tell which is which. Git history and the
  `pre-doc-cleanup-2026-08` tag hold the archive.
- If code cites a document, the citation must resolve. `nix flake check` enforces
  this.
