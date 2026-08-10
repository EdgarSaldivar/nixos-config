# Follow-ups — handoff for Codex

Written 2026-08-10 after the docker→k3s migration completed (35/35, docker at zero) and
after an outage that night. **Read `K3S-HANDOFF.md` "▶ START HERE" and
`TRAEFIK-CUTOVER-RUNBOOK.md` first.** Three tasks below, in priority order.

---

## ⚠️ READ BEFORE TOUCHING ANYTHING — deploy rules that have caused real outages

- **`nixos-rebuild` cannot run from the Mac.** `rsync` the tree to the host and run its
  native `nixos-rebuild` with an **absolute** flake path. Never `~` under sudo.
- **Three beats, and the third gets skipped:**
  `rsync (uncommitted) → build/test on host → commit → RSYNC AGAIN → switch`.
  A switch that redeploys the config the host already had is **indistinguishable** from a
  real deploy in its output.
- **Flakes only see tracked files.** `git add` before building.
- **A commit can span TWO hosts.** `manifests/*` and `pelargir/*` are delivered by
  **pelargir**; `traefik-routes.nix` and `system.nix` by **minas**. Rebuild **pelargir
  first** for manifest changes. Rebuilding minas alone once caused the only outage of the
  migration.
- **Adding a new namespace + its Secret in one commit makes `nixos-rebuild switch` exit 4.**
  It self-heals on the `k3s-apply-secrets` restart. Verify, don't panic.
- ssh alias is **`minas`**, not `minas-tirith`. `docker` needs sudo there and silently
  reports **zero** containers without it.
- **⛔ traefik is now the live public ingress as a k3s Pod.** Any change to `spec.template`
  in `manifests/traefik.yaml` triggers a rollout, and with `strategy: Recreate` on a pinned
  singleton that is a **full ingress outage**, not a rolling update. Run
  `kubectl diff -f <candidate>` first — **empty output is the proof** there is no template
  change. Rollback is armed at
  `/storage2/backup/traefik-maintenance-20260810T085521Z/`.

### Probes that lie on this fleet — each cost hours

- **`ss` is blind to k8s `hostPort`** (CNI PREROUTING DNAT). It reported 8081 free while
  calibre owned it. Cross-check the **iptables NAT table**.
- **`bash </dev/tcp/HOST/PORT` returning success proves only that SOMETHING answered**,
  never that the intended host did. A router/middlebox completes the handshake. This exact
  false fact sent two sessions down the wrong path.
- **An access log is not a packet trace.** It cannot record a ClientHello that never arrives.
- **⛔ An asymmetric-routing failure leaves NO conntrack entry, increments NO iptables
  counter, and never matches the CNI hostPort DNAT rule** — so every first-line tool says
  "the packet never arrived" while `tcpdump` plainly shows it arriving with the correct
  destination MAC. **When those two disagree, run `ip route get <peer>` immediately.** That
  one command was the answer to the 2026-08-10 outage and it was found last, not first.
- `tcpdump` is NOT installed on minas; use `nix-shell -p tcpdump --run '...'`.
- minas has **no `openssl`, `jq`, `dig`, or python `cryptography`**. python3 stdlib only.

---

## TASK 1 — ✅ COMPLETE: external reachability + certificate-expiry monitoring

**Why:** on 2026-08-10 the entire public ingress was down and **nothing on the fleet
noticed** — the owner did. Kubernetes readiness cannot detect this by construction: traffic
arrives on `hostPort`, not through a Service, so readiness withdraws nothing and gates
nothing. `/ping` proves the process answers; it does not prove any hostname resolves,
routes, or serves.

**Goal:** an alert that fires when the public hostnames stop being reachable *from outside*,
or when the wildcard certificate approaches expiry — both independent of k8s health.

**Completed 2026-08-10.** Commit `46868cb` installed a five-minute external probe on
pelargir. It reproduces all recorded statuses, requires public DNS, checks the stable
wildcard identity, pages below 21 days, and suppresses the first two consecutive failures.
Healthy scheduled runs return `matched=56 drifted=0 missing=0 errors=0`.

**Constraints / invariants:**
1. The probe MUST originate somewhere that is genuinely external to minas' network.
   **`pelargir` is the correct vantage point** — different site, different public IP, routes
   to minas over the open internet (not the tunnel). Probing from minas or from the
   workstation is invalid: the workstation's default route is the WireGuard tunnel and
   hairpins, reporting success regardless.
2. ⛔ **Acceptance is the recorded baseline reproduced, never "a 200".** Five hostnames are
   healthy at **401**; `maintainerr.saldivar.io` returning 200 means its `basic-auth@file`
   middleware was dropped. Baseline lives at
   `/storage2/backup/traefik-precutover-baseline-20260810T051832Z.txt`.
3. `dungeon.saldivar.io` is `000ERR` **and was already dead pre-migration**. It must stay
   excluded/inverted, not "fixed".
4. Certificate check must alert on the **wildcard** (`*.saldivar.io` + `saldivar.io`,
   issuer Let's Encrypt). **Decision 2026-08-10: page at 21 days remaining.** Traefik starts
   renewal at 30 days; a 45-day page would be red before normal renewal every cycle. The
   21-day threshold gives automatic renewal nine days to work and still leaves three weeks
   for manual recovery.
5. Must not page on a single transient failure; require consecutive failures.
6. Reuse `hosts/nixos/minas-tirith/scripts/ingress-acceptance.py` if practical — it already
   does status-vs-baseline, per-SNI fingerprint, port-80 redirect and `@file` router
   correlation, and is stdlib-only. ⚠️ It is written to run ON minas; an external variant
   must not assume localhost.
7. Existing monitoring lives in `hosts/nixos/minas-tirith/monitoring.nix` — follow its
   conventions rather than inventing a parallel system.

**Acceptance completed in the owner-approved 2026-08-10 window:** commit `d66df41`
declaratively scaled traefik to 0; the installed manifest, live Deployment, AddOn checksum,
`AppliedManifest` event and absent CRI task all agreed. Three forced checks advanced the
counter `1 → 2 → 3`; only the third sent `/fail`, and Healthchecks accepted it. Commit
`099d4d2` restored traefik, the recovery run returned 56/56, reset the counter to 0 and sent
the successful recovery ping.

---

## TASK 2 — ✅ ALREADY CLOSED: replicas drift (audited 2026-08-10)

This item was stale when the handoff was written. Commit `a986e6f` had already corrected
the real list: **13 manifests**, not 15 (`animearr`, `calibre`, `flaresolverr`, `kavita`,
`lidarr`, `maintainerr`, `overseerr`, `prowlarr`, `radarr`, `shelfmark`, `sonarr`,
`tautulli`, and `wrapperr`). `audiobookshelf`, `komga`, and `palworld` already declared 1
and were not drifted.

The Codex follow-up audit checked the repository, pelargir's installed files, the live
Deployment specs, AddOn checksums and retained k3s journal events. Every corrected AddOn
has an `ApplyingManifest` → `AppliedManifest` pair; the affected Pods were not recreated by
that apply, and all currently have zero restarts. The only CronJob, `jellyfin-quiesce`, is
declared and live at `suspend: false`. No new deploy was needed for this task.

**Why:** every migration before jellyfin shipped its manifest at `replicas: 0` and then
scaled imperatively. Those manifests and the cluster **permanently disagree**. k3s
auto-deploy re-applies a manifest when its **file checksum changes** *or* when the **server
restarts**, at which point the declared `0` is reasserted and the service goes down with
nothing in git to explain why. traefik was fixed on 2026-08-10; the rest were not.

Verified examples in that state: `kavita`, `komga`, `calibre`, `tautulli`. **Enumerate the
real list; do not trust that number** — this repo's counts have gone stale twice.

**Constraints / invariants:**
1. For each service: set the manifest to the replica count it is **actually running**, then
   deploy and **verify BOTH sides**.
2. ⛔ **"Both sides read N" can FALSE-PASS.** The live object is already N from the
   imperative scale, so it reads N whether or not k3s applied the new file at all — a failed
   apply is indistinguishable from success. **Also check the AddOn**:
   `kubectl -n kube-system get addon <name> -o yaml` and its events for `AppliedManifest`.
3. ⛔ Run `kubectl diff -f <candidate>` per service first. If anything under `spec.template`
   differs, that service will **restart** when deployed — batch those into an agreed window
   rather than doing them silently.
4. ⛔ **CronJobs are worse than Deployments here**: a reverted `suspend: true` silently stops
   taking backups, and the staleness gate would not report it for two days. Check for any.
5. Do these in small batches with verification between, not one sweeping commit.

**Acceptance:** for every service, installed manifest replica count == live replica count,
AddOn reports a successful apply, and no Pod was restarted except where explicitly intended
and agreed.

---

## TASK 3 — ✅ COMPLETE: wire `ingress-acceptance.py` into the NixOS config

**Completed 2026-08-10.** Commit `46868cb` packages the repository script with an absolute
Nix Python shebang and an offline build-time self-test, and installs it in minas' system
profile. The obsolete `/root/ingress-acceptance.py` hand-copy was removed after deployment.

**Constraints / invariants:**
1. Install it from the repo path so the host copy cannot diverge.
2. Keep it **executable by root on minas** — it must run there (hairpin NAT makes off-host
   probing lie) and reads `/storage2/backup/...` baselines.
3. Python 3 **stdlib only** — do not add `cryptography`, `requests`, `yaml` or `urllib3`;
   they are absent on the host by design and the script hand-decodes DER because of it.
4. `--selftest` must keep passing offline (it opens no sockets). Use it as the build/CI gate.
5. This is a **minas**-owned change (`system.nix` or a small module), so rebuild minas — not
   pelargir.

**Acceptance completed:** the installed command prints `SELFTEST OK`; its strict live run
returns `matched=55 drifted=0 missing=0 errors=0`.

---

## Smaller items, not worth their own task

- ✅ `TRAEFIK_BASIC_AUTH_CREDS` was removed in commit `099d4d2` from the Deployment, rendered
  Secret and sops. Kubernetes apply retained the omitted live list/data entries, so the env
  item was patched away while replicas were zero and the retained Secret data key was
  patched after recovery; both are verified absent from the final live state.
- ✅ `/etc/letsencrypt/acme.json` is covered by the nightly `/etc` mirror and ZFS snapshots.
  Before the maintenance rollout a fresh four-file capture was verified at
  `/storage2/backup/traefik-maintenance-20260810T085521Z/` (sha256 `31f2b822…`); it remained
  byte-identical after scale-down and recovery. Re-capture before a later risky change
  because a restore point becomes stale after renewal.
- ✅ `__pycache__/` and `*.py[cod]` are ignored; the existing local cache was removed.
- Docker decommission: the plan keeps compose files and images **30 days** post-cutover
  (≈ **2026-09-09**) before removing `virtualisation.docker`. 51 images and ~12 exited
  containers remain, deliberately.
- `dungeon.saldivar.io` has an unreachable backend (`192.168.6.94`) and has been dead since
  before the migration. Not a regression; decide whether to retire the routes.
- ⛔ **Open architectural decision, unresolved:** pelargir is a cluster-wide control-plane
  SPOF (one SQLite control plane, at the owner's house). Adding minas as a second member does
  NOT fix it — a two-member quorum is not fault-tolerant. One cluster per house is the honest
  answer. Recorded in `INGRESS-ARCHITECTURE.md`. Do not start this without the owner.
