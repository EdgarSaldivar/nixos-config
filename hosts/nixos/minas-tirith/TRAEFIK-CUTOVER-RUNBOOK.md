# traefik cutover — RUNBOOK

**Status as of 2026-08-09: both blocking CRITICALs are CLOSED, and the Pod has now
run.** What remains is scheduling an outage window. Read `K3S-HANDOFF.md` "▶ START
HERE" and `INGRESS-ARCHITECTURE.md` first — this file is the procedure, not the
context.

`traefik` is the **last** workload on docker. 34 of 35 are on k3s.

---

## 0. What the canary proved, so it is not re-litigated

Two canary Pods ran on 2026-08-09 while docker kept serving production, then were
deleted. Both are committed (`manifests/traefik-canary.yaml`,
`manifests/traefik-canary-b.yaml`) and are deliberately **absent from
`pelargir/manifests.nix`** — they are applied with `kubectl apply -f` and deleted.

| question | answer | evidence |
|---|---|---|
| Does the Pod start at all? | **Yes** — 1/1 Running, 0 restarts, in 8s | it had NEVER started before |
| Do both hostPath mounts resolve? | Yes | Pod would not have started |
| Does `/ping` answer kubelet? | Yes | no nextcloud-style Host-header trap |
| Do all 23 route files parse? | Yes | no parse errors; 23 distinct routers observed |
| Does it serve the ingress correctly? | **All 26 hostnames reproduced the baseline EXACTLY** on port 18443 | incl. `maintainerr`/`traefik` at 401 (auth intact) and `dungeon` still dead |
| Right certificate per SNI? | 25/25 reachable hosts matched fingerprint `AF:32:16:D5…` | 26th is `dungeon`, which never connects |
| Port 80 → HTTPS redirect? | Yes, 301 to the same host | tested on 3 hostnames |
| Any route served by a non-`@file` router? | **No** — 23 routers, all `@file`, zero `@docker` | so dropping the docker provider loses nothing |
| Does the Cloudflare token still WORK? | **Yes, proven today** | pass B: fresh account registered against LE **staging**, DNS-01 TXT written AND cleaned via the Cloudflare API, `Validations succeeded`, `Server responded with a certificate` |
| Did anything touch the production ACME store? | **No** — byte-identical throughout | sha256 `31f2b822…` before and after both passes |

⚠️ **Why pass B mattered even though the token hashes identical to docker's:** a revoked
or expired token hashes the same. The live wildcard expires **Sep 16** and traefik renews
at 30 days remaining, so the first real exercise would otherwise have been **~Aug 17**,
unattended. That is now known-good ahead of time.

---

## 1. CRITICAL 1 — CLOSED: replicas promotion is a separate, third phase

**The hole:** scaling imperatively to 1 and committing `replicas: 1` in the same breath is
incoherent. If the manifest declaring `1` is delivered, a rollback that scales the API
object to 0 gets undone — pelargir's activation reasserts `1` on the next checksum change
or k3s restart, starting the Pod *beside* docker: competing ingress rules and **two writers
on `acme.json`**.

**The fix is an ordering rule with a verification gate.**

| phase | deployed manifest says | live Deployment | commit state |
|---|---|---|---|
| **A** pre-cutover (now) | `replicas: 0` | 0 | `0` committed |
| **B** cutover | `replicas: 0` — *unchanged* | 1, set **imperatively** | still `0` committed |
| **C** durable promotion — **separate change, after acceptance** | `replicas: 1` | 1 | `1` committed + deployed |

- In **phase B**, `kubectl scale` only. **Do not commit `1`.** The declared 0 is what makes
  rollback a single authoritative action.
- **Phase C is not optional** — leaving it undone is how this fleet already has 15 services
  one k3s restart from an outage. But it happens *after* the service is accepted, as its
  own commit, and it must verify **both** sides:

```sh
# on pelargir, after committing replicas: 1 and rebuilding
grep -A2 '^spec:' /var/lib/rancher/k3s/server/manifests/minas-traefik.yaml | grep replicas
sudo k3s kubectl -n traefik get deploy traefik -o jsonpath='{.spec.replicas}{"\n"}'
# BOTH must read 1. One reading 1 and the other 0 is the drift this phase exists to prevent.
```

⛔ **Rollback rule:** if the cutover is rolled back after phase C has been committed but not
yet shipped, **revert that commit too**. A `replicas: 1` sitting in git is a scheduled
outage waiting for the next deploy.

---

## 2. CRITICAL 2 — CLOSED: `acme.json` rollback protection

**The hole:** both runtimes mount `/etc/letsencrypt` read-write. Sequential ordering prevents
*concurrent* corruption but does nothing about a failed issuance, account update or partial
write made by the k8s process before a rollback — docker then inherits the damaged file.

⛔ **And the store is bigger than the handoff assumed.** It holds **10 certificates**, not one
wildcard: `saldivar.io` + `*.saldivar.io`, **seven for `roadmastertransport.io`** (a different
zone, with no routes on this host — orphans), `dungeon.pelargir.saldivar.io`, and
`admin.pin.saldivar.io`. **Restore the whole file. Never reason about one certificate.**

### Known-good baseline, recorded 2026-08-09

| | |
|---|---|
| sha256 | `31f2b8229b4d29b49be6265490f32da45f682d1405c665268e1b156f016d0f55` |
| size / owner / mode | 128998 bytes, `root:root`, `0600` |
| wildcard fingerprint | `AF:32:16:D5:E9:6C:3B:84:DF:02:47:D5:E6:F3:79:A2:89:38:96:C7:65:38:83:7E:A3:22:9B:39:6D:27:5A:82` |
| issuer / expiry | Let's Encrypt `CN=YR1`, notAfter **Sep 16 02:41:50 2026 GMT** |

⚠️ Re-take this at cutover — it is cheap, and the recorded hash above is only valid until
traefik next writes the store.

### Capture (after docker is stopped and PROVEN gone, before the Pod starts)

```sh
TS=$(date -u +%Y%m%dT%H%M%SZ); D=/storage2/backup/traefik-cutover-$TS
sudo mkdir -p "$D"
sudo install -m 600 -o root -g root /etc/letsencrypt/acme.json "$D/acme.json.known-good"
sudo sha256sum /etc/letsencrypt/acme.json | sudo tee "$D/acme.json.sha256"
sudo stat -c '%U:%G %a %s' /etc/letsencrypt/acme.json | sudo tee "$D/acme.json.meta"
```

### Restore (rollback path)

```sh
# 1. Prove the Pod's container task is GONE via the CRI — `ss` is blind to CNI hostPort DNAT.
sudo k3s kubectl -n traefik scale deploy/traefik --replicas=0
sudo k3s crictl ps -a --name traefik            # expect no Running task
# 2. Preserve the suspect file for diagnosis — do not overwrite it in place.
sudo mv /etc/letsencrypt/acme.json /etc/letsencrypt/acme.json.failed-$TS
# 3. Restore atomically, same filesystem, identical metadata.
sudo install -m 600 -o root -g root "$D/acme.json.known-good" /etc/letsencrypt/acme.json.new
sudo mv /etc/letsencrypt/acme.json.new /etc/letsencrypt/acme.json
# 4. Re-validate BEFORE starting docker.
sudo sha256sum -c "$D/acme.json.sha256"
# 5. Only now:
sudo docker start <RECORDED CONTAINER ID>
```

⛔ Rollback is **`docker start <id>`**, never `docker compose up`. A compose recreate rebuilds
the container from today's file and applies every drift accumulated since it was created.

---

## 3. Pre-flight

```sh
# Re-record the container identity — do NOT trust a previously written id or name.
ssh minas 'sudo docker ps --format "{{.ID}} {{.Names}} {{.Status}}"'    # expect exactly 1

# Re-verify only pelargir needs a rebuild (it was true on 2026-08-09; re-check, do not assume)
git diff --name-only <deployed-rev>..HEAD -- . ':(exclude)*.md'
# every path must be pelargir-delivered: manifests/*, pelargir/*, secrets/*

# Re-take the ingress baseline, then re-take acme.json metadata (section 2).
```

⚠️ The image is already in containerd and resolves by digest — verified. Do not skip this
check on a rebuilt node: without it, the first pull happens *during* the outage.

---

## 4. Cutover

⛔ **This is atomic and cannot be staged.** traefik *is* the router, so there is no
lower-priority route to hide behind. From `docker stop` until the Pod serves, all 26
hostnames are down. Expected window: **minutes**, dominated by proving docker is gone.

1. **Rebuild pelargir first** (delivers the manifest), confirm, *then* minas if it needs it.
   Rebuilding minas alone published routes for Deployments that did not exist and caused the
   only outage of this migration.
2. `docker stop traefik` — then **prove** it is gone: `Running=false`, `Pid=0`, no NAT rule,
   no docker-proxy socket. Record the id.
3. Capture the `acme.json` known-good set (section 2).
4. `sudo k3s kubectl -n traefik scale deploy/traefik --replicas=1` — **imperative only**.
5. Accept on **external** checks, not readiness:

```sh
sudo python3 /root/ingress-acceptance.py \
  --baseline /storage2/backup/traefik-precutover-baseline-<TS>.txt \
  --target 127.0.0.1:443 --http-target 127.0.0.1:80 \
  --expect-fingerprint AF:32:16:D5:...:5A:82 \
  --access-log <traefik access log>
# Requires matched=N drifted=0 missing=0 errors=0 and exit 0.
```

⛔ **Readiness is not a cutover gate.** Traffic arrives on hostPort, not through a Service,
so readiness withdraws nothing. `/ping` proves the process answers; it does **not** prove the
file provider loaded, that a usable certificate exists, or that any backend resolves. A
malformed route file yields a Ready Pod serving 404s.

⛔ **Acceptance is the recorded baseline reproduced, never "a 200".** Five hostnames are
healthy at 401; `maintainerr.saldivar.io` returning 200 means its `basic-auth@file`
middleware was dropped. `dungeon.saldivar.io` is `000ERR` and was **already dead** before the
migration — that is not a regression.

6. Soak. **Then** phase C (section 1).

## 5. The acceptance tool

`scripts/ingress-acceptance.py` — Python 3 **stdlib only** (minas has no `openssl`, `jq`,
`dig`, or `cryptography`; it hand-decodes DER). Run it **on minas as root**: hairpin NAT makes
off-host probes lie.

- `--selftest` runs offline and needs no fleet access.
- Validated against live production 2026-08-09: `matched=55 drifted=0 missing=0 errors=0`.
- ⚠️ `--access-log` needs **JSON** logs. The production manifest now sets
  `--accesslog.format=json` for exactly this reason; the `accessLog: format: json` in
  `traefik.yml` is a static key in a dynamic file and is silently ignored.
- Hosts baselined `000ERR` are excluded from the fingerprint and router checks — they cannot
  produce either, and a gate that always fails is worse than no gate.

## 6. Known cosmetic items, deliberately NOT changed

- `delayBeforeCheck is now deprecated` — the current form is **proven working** by canary
  pass B. Do not "fix" a warning on the untested path during a cutover.
- `TRAEFIK_BASIC_AUTH_CREDS` is **dead config**: `traefik.yml` hardcodes the real bcrypt hash,
  and nothing reads the env var. The value carried in sops is a junk placeholder — copied
  faithfully from the live docker container, which also carries it unused. Harmless today;
  remove it rather than wiring it up, and never let it become the dashboard credential.
