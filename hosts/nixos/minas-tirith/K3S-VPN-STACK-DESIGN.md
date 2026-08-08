# VPN-gated stack on k3s — design, not a port

**Status: DESIGN v3, nothing built.** Supersedes the "translate binhex faithfully" approach
for the three VPN-gated groups. Written 2026-08-08 after the owner redirected the work:
*"move away from direct migration/preservation to how IT SHOULD be done… the goal is a
port yes but also an improvement, not just a port for porting sake."* Breakage is to be
avoided but is not fatal — fix forward.

v1 was REJECTED in cross-review with 4 blockers. Every finding is folded in below, and the
ones that were **my errors** are called out where they sit, because each is a trap someone
could repeat. Read `K3S-HANDOFF.md` first for fleet conventions and the deploy discipline.

---

## Why this is a redesign and not a translation

`deluge-books` today is `binhex/arch-delugevpn`: **privileged: true** (`CapEff`
`000001ffffffffff` — every capability the kernel has), bundling OpenVPN + Deluge +
Privoxy + supervisord + a bespoke `init.sh` kill-switch, with an entrypoint override that
symlinks `iptables*` → `xtables-nft-multi` because NixOS ships no legacy x_tables. It
writes its VPN credentials to `/config/openvpn/credentials.conf` on a hostPath, and
carries a literal MyAnonaMouse session cookie **in its argv**.

`gluetun` runs on the same host, today, doing the same job:

| | binhex (deluge-books) | gluetun |
|---|---|---|
| privileged | **true** — all capabilities | **false** |
| capabilities | — | `CAP_NET_ADMIN` only |
| `/dev/net/tun` | implicit via privileged | explicit device |
| kill-switch | bespoke `init.sh` | maintained upstream, documented never to deactivate |
| credentials | written to disk | **`*_SECRETFILE` for every one** |
| iptables on NixOS | needs an entrypoint shim | works as shipped |
| DNS | host resolvers | **own DoT resolver over the tunnel** |

Three concrete defects go away: the privileged container, the credential file on disk, and
a hand-rolled kill-switch we would own forever. `qbittorrent-books` and
`flaresolverr-books` already run as `NetworkMode=container:<gluetun>`, which is exactly a
shared Pod netns — the pattern is already load-bearing here, just under Docker.

⚠️ gluetun today sets `OPENVPN_USER`/`OPENVPN_PASSWORD` as **env**; the `*_SECRETFILE`
variables are at their defaults and unused. Adopting the file path is the single change
that makes the secret goal real.

---

## Topology — three groups, not one Pod

⛔ **Do not build one mega-Pod.** PIA's port-forward assigns **one port per connection**,
and all containers in a Pod share one address and port space. Two torrent clients cannot
both bind the same peer port. There are **three** torrent clients here, not two.

| Pod | containers | ingress today (Docker label ONLY) |
|---|---|---|
| `deluge-books` | gluetun + deluge + **MAM registrar** | `btbooks.saldivar.io` → 8112 |
| `deluge-vpn` | gluetun + deluge | `bt.saldivar.io` |
| `qbittorrent-books` | gluetun + qbittorrent + flaresolverr | `books-dl.saldivar.io` → 8080 |

flaresolverr needs no inbound peer port, so sharing is safe there.

❓ **Preflight before building**: prove PIA issues **three simultaneous** usable port
forwards on this account. The three-Pod split is sound in principle, but capacity is
unverified and it is cheap to test first.

---

## Egress control — the part v1 got wrong

⛔ **v1 proposed allowing `10.42.0.0/16` and `10.43.0.0/16` in
`FIREWALL_OUTBOUND_SUBNETS`. That was wrong in both directions and is rejected:**

- **Unnecessary**: gluetun already ACCEPTs output to each *directly attached* subnet
  before it applies configured outbound subnets. The node's Pod subnet is reachable
  without the entry.
- **Over-broad**: a `/16` permits reaching **every Pod on every node**, so any HTTP/SOCKS
  relay anywhere in the cluster becomes a VPN bypass.
- **Self-contradictory**: v1's acceptance simultaneously demanded "DNS fails when the
  tunnel is down" and "cluster DNS works". With `10.43/16` allowed,
  `dig @10.43.0.10 x.attacker.example` succeeds *through CoreDNS's own non-VPN egress*
  while the tunnel is down — and DNS tunnelling carries arbitrary data. Both criteria
  cannot hold.

### The resolution: these Pods need no cluster egress at all

gluetun runs its **own DNS server** (`DNS_SERVER=on`, DoT upstream, `DNS_UPSTREAM_IPV6=off`)
and resolves **over the tunnel**. So the app containers must use gluetun's resolver, not
CoreDNS, and therefore need nothing from `10.43/16`.

✅ Confirmed in review: nothing else here needs cluster egress — image pulls happen from
the **node**, kubelet probes / traefik / prowlarr / readmeabook are all **inbound** flows,
replies are stateful, and MAM plus application traffic belong on `tun0`.

⛔ **"Apps use gluetun's DNS" is an assertion until it is CONFIGURED.** Pods default to
CoreDNS, so without an explicit Pod-level setting the registrar resolves MAM via
`10.43.0.10`, the request is dropped, its startup probe never passes, and **Deluge never
starts**. Set `dnsPolicy: None` with `dnsConfig.nameservers: [127.0.0.1]`, and verify
against the pinned gluetun image. Allowing CoreDNS instead would reinstate the DNS-bypass
defect this section exists to remove.

- `FIREWALL_OUTBOUND_SUBNETS`: **empty**. Not the cluster CIDRs, not the LAN.
- ⛔ **The egress NetworkPolicy must not be a bare default-deny.** It applies to the whole
  Pod including **gluetun's own OpenVPN connection**, and it cannot match a process or
  `tun0`. A default-deny policy means gluetun can never reach PIA, its startup probe never
  passes, and all three Pods sit in init **forever**. The policy must allow the selected
  PIA endpoint IP/protocol/port *before the tunnel exists*.
  - PIA endpoint IPs come from gluetun's built-in server data, so cluster DNS is **not**
    required for bootstrap. A hostname-based or custom endpoint would need pre-resolution,
    because gluetun deliberately prevents initial DNS leakage.
  - ⚠️ Define how endpoint IPs are pinned and refreshed. A broad port-only allowance would
    let every container attempt direct traffic on that port, leaving gluetun's in-netns
    firewall as the only discriminator.

### Inbound — and why `FIREWALL_INPUT_PORTS` is NOT the boundary

⛔ v2 said inbound was "governed by `FIREWALL_INPUT_PORTS`". **Wrong.** gluetun
automatically installs `INPUT -i eth0 -d <local-subnet> -j ACCEPT`, so traffic addressed to
the **Pod IP** is already accepted before per-port rules apply. **NetworkPolicy is the
actual port and source boundary** for these Pods — which makes getting its ingress rules
right load-bearing in a way v2 did not recognise.

Consequences to get right:

- ⛔ **A Pod-selector-only ingress rule would drop public ingress with 502s.** The edge is
  **Docker Traefik** (container `172.16.1.2` on `traefik-net`), *not* a selectable
  Kubernetes Pod, and it reaches backends as
  `http://<name>.<namespace>.svc.cluster.local:<port>`. Its **post-DNAT/SNAT source must
  be measured against a real Pod** and represented explicitly as an `ipBlock` — do not
  guess between the Docker container address and a SNAT'd node address.
- Kubelet probes are **local-node** inbound and are exempt from policy anyway.
- prowlarr and readmeabook are ordinary Pods and can be allowed by selector.
- An unenforced or mistaken policy exposes **every** listening application port that
  gluetun has already accepted — so this is a positive-and-negative test at cutover, not a
  write-and-hope.
- **NetworkPolicy** narrows Pod-to-Pod reachability and is the *effective* port/source
  boundary for traffic addressed to the Pod IP (see the inbound section — gluetun's own
  input rules are not that boundary). k3s enforces it by default (kube-router, in-process
  — no separate Pod, so its absence from `kube-system` is expected, not evidence it is
  off).

✅ **Enforcement VERIFIED on this cluster 2026-08-08** — positive, negative and
reversibility tested with throwaway Pods on minas; see open question 6.

### ⛔ ACCEPTED RESIDUAL RISK: the local-node relay cannot be closed here

v2 claimed a NetworkPolicy "closes the same-node relay path". **That was wrong.**
Kubernetes explicitly **exempts traffic between a Pod and its local node** from
NetworkPolicy. gluetun independently ACCEPTs output to every directly attached network,
including the Pod subnet and the node gateway. So the two enforcement points **do not
compose**: a process inside the Pod can reach `10.42.x.1` or another local-node address,
and the node forwards it out its ordinary internet route — with the tunnel down and the
policy in place. `FIREWALL_OUTBOUND_SUBNETS=` does not prevent this.

**Decision (owner, 2026-08-08): document and accept.** Reasoning, recorded so it can be
revisited rather than rediscovered:

- Exploiting it requires **code execution inside the container AND an attacker-controlled
  relay on the node**. It is a compromised-container escape, not a VPN-failure leak.
- The kill-switch's actual job is unaffected: when the tunnel drops, gluetun's `OUTPUT
  DROP` means the torrent client cannot reach the internet directly. That is the failure
  mode that actually occurs.
- ⚠️ **What runs today is strictly worse on both counts**: binhex allows all of
  `10.0.0.0/8` — the entire LAN, not just the node — and runs **privileged**, so a
  compromise there needs no relay trick at all. This design is a large net improvement
  even with this hole open.

If this is ever revisited, the fix is host-level: NixOS-side nftables containment on minas
(host firewalling is already the NixOS side of the seam), or stronger node-level
segmentation for VPN workloads. Neither is in scope here.

### The control server

⛔ **"Do not expose `:8000`" is NOT achieved by omitting a Service.** Pods reach Pod IPs
directly. gluetun's control server can stop or reconfigure the VPN, so a compromised Pod
reaching `10.42.x.y:8000` is a kill-switch bypass. Bind it to **loopback**
(`HTTP_CONTROL_SERVER_ADDRESS=127.0.0.1:8000`) and configure least-privilege auth. The
NetworkPolicy is the second layer, not the only one.

---

## The gluetun sidecar

gluetun runs as a **native sidecar**: an `initContainer` with `restartPolicy: Always`
(k3s v1.35.6, so GA).

⛔ **A native sidecar alone does NOT guarantee the firewall is up before the app starts.**
Kubernetes proceeds once a restartable init container is merely *running* — unless it has
a **passing `startupProbe`**. gluetun therefore gets an exec `startupProbe` on its own
healthcheck, plus a readiness probe contributing to Pod readiness.

⚠️ **That closes the INITIAL window only.** After startup succeeds, an independent gluetun
restart does **not** stop Deluge — readiness merely removes Service endpoints, and
already-running processes and established connections continue. **The firewall is the
safety boundary; the probe is not.** This is why acceptance below is adversarial and
continuous rather than a rule snapshot.

### Restart semantics

Containers share the netns; the sandbox outlives any individual container. Netfilter rules
are **namespace** state, not process state. So if gluetun crashes while the sandbox lives,
its DROP rules remain and the vanished tunnel leaves the apps fail-closed. If the sandbox
is replaced, the apps go with it and gluetun starts first in the new one. The dangerous
moment is the **transition**, when a replacement gluetun mutates or restores rules.

### Capabilities — a test matrix, not an assertion

⛔ v1 asserted `drop: [ALL]` for the app containers. **Unproven and probably wrong**:
binhex images do PUID/PGID `chown` work at startup, and gluetun's public-IP writer chowns
too, so `CHOWN`/`SETUID`/`SETGID` are likely required. Checking only for `NET_RAW` was
insufficient.

Determine empirically against the exact pinned digests, adding only the proven minimum:

| container | start from | likely additions to test |
|---|---|---|
| gluetun | `drop: [ALL]` + `NET_ADMIN` | `NET_RAW` (ICMP health), `CHOWN` (public-IP writer) |
| deluge / qbittorrent | `drop: [ALL]` | `CHOWN`, `SETUID`, `SETGID` (PUID/PGID init) |
| flaresolverr, registrar | `drop: [ALL]` | expected none |

Also: `allowPrivilegeEscalation: false`, `RuntimeDefault` seccomp, `/dev/net/tun` as
`hostPath` `type: CharDevice`, `automountServiceAccountToken: false`, every image
**digest-pinned**.

---

## Manifest invariants — required, and absent from v1

⛔ v1 omitted these. Without them a routine update runs **two Deluge processes on one
hostPath**, or a Pod lands on pelargir and presents empty state:

- `nodeSelector: kubernetes.io/hostname: minas-tirith` — hostPaths are local to minas.
- `strategy: Recreate` — never two writers on one state tree.
- Typed hostPaths: `type: Directory` (⛔ the default **creates** a missing path, turning a
  typo into an empty config and a "fresh" client), `/dev/net/tun` as `CharDevice`.
- `enableServiceLinks: false`, `terminationGracePeriodSeconds` sized for a clean Deluge
  shutdown, explicit `resources`, and application-specific readiness (not TCP-only).
- Declared replica counts matching reality — this repo has been bitten by manifests saying
  `replicas: 0` while running at 1.

---

## Secrets

Delivery: sops → rendered to tmpfs on pelargir → applied by `k3s-apply-secrets` → mounted
as a **Secret volume** → consumed via gluetun's `*_SECRETFILE` variables.

- ⛔ **Not `secretKeyRef`** — resolved env reaches containerd's on-disk metadata.
- ⛔ **Mount the whole Secret directory — never `subPath`**, which never receives updates,
  so a rotation would appear to succeed and change nothing.
- Repo-wide `secretKeyRef` remains only in `tracearr.yaml` and `palworld.yaml`; convert
  when those are next touched, not as a campaign.

### The guarantee, stated so it is actually true

⛔ v1 claimed "deployment secrets are never materialized into persistent runtime metadata
or manifests." **Still overclaiming**: the Secret is a persistent API object in the k3s
datastore, and a client-side `kubectl apply` can persist the submitted content in the
last-applied annotation.

✅ Accurate: **plaintext deployment values are not written by this delivery path into Git,
the auto-deploy directory, containerd metadata, or unencrypted node files.** An API reader
can still retrieve the Secret, applications still persist their own credentials into
hostPath config and logs, and `.torrent` files carry tracker announce tokens regardless.
"No plaintext anywhere on disk" needs encrypted datasets (`storage2` is `encryption=off`,
and ZFS encryption is creation-time only) and is tracked as separate work.

### Rotation is one transaction

⛔ `k3s-apply-secrets` does not react to value-only sops rotations, and applying a changed
Secret does **not** restart gluetun, which reads credentials at startup. A half-done
rotation leaves the old credential live while looking complete:

`sops edit` → `systemctl restart k3s-apply-secrets` (**explicitly required**) → confirm
the Secret's `resourceVersion` changed → recreate affected Pods **one at a time** → prove
tunnel up and exit IP non-local between each.

---

## The MAM registrar

MAM sessions are **IP-bound**. Today a literal `mam_id=` cookie in argv calls
`/json/dynamicSeedbox.php` after the tunnel comes up. Dropping it breaks MAM **not at
cutover but at the next exit-IP change** — silently, and looking unrelated.

⛔ **Do not use gluetun's port-forward hook for this.** It fires on "port forwarding
established", not "exit IP changed", so registration would stop silently if forwarding
failed while egress stayed usable.

The registrar is a **native sidecar ordered after gluetun** — ⛔ v1 said "sidecar" without
specifying, and on an ordinary application sidecar a startup probe gates only Pod readiness
while Deluge starts concurrently, so the intended gate would not exist. It:

- mounts **only** the MAM Secret, and performs the HTTP **itself** so the cookie never
  reaches a child argv or a log;
- watches gluetun's `PUBLICIP_FILE` (`/tmp/gluetun/ip`) through a shared **memory-backed**
  `emptyDir`. ⛔ gluetun **truncates that file to empty on disconnect and rewrites it
  non-atomically** — so parse defensively and ignore empty/partial reads, or the registrar
  will submit garbage transitions;
- persists a **current-IP success marker** so backoff survives its own restart. ⛔ A finite
  failed startup probe restarts the container and would otherwise reset process-local
  backoff, turning a MAM outage into a restart loop that pounds MAM while Deluge never
  starts;
- gates Deluge on first successful registration via `startupProbe`.

❓ MAM's retry/rate-limit behaviour is unknown and must be measured before fixing the poll
interval and backoff ceiling.

### Forwarded-port updates

⛔ gluetun's port-forward hook can fire **before** the gated torrent container is
listening; that single update fails and may not retry until the next reconnect, leaving
the client announcing a stale port indefinitely while its UI looks healthy. Use a
**retrying watcher** with a current-port readiness gate, not a one-shot hook.

---

## Deluge state — the procedure v1 got wrong

Measured live: **Deluge 2.1.1, libtorrent 2.0.10.0**, Python 3.13.1, **47 torrents**,
`state/torrents.state` present, tree owned `1000:1000`.

⛔ **v1's ordering destroyed the thing it was preserving.** It said *"Pause everything;
record … queue state"* — recording **after** pausing means the record and the copy both
say all 47 torrents are paused, and that is how they come back. v1 also said "complete
tree" and then gave a whitelist that omitted root-level plugin config while separately
claiming labels survive.

Corrected procedure:

1. **Record identity FIRST, before touching anything**: torrent count, infohashes, save
   paths, queue order, **and per-torrent paused/active state**.
2. Pause everything, then stop the container cleanly.
3. Copy the **entire tree** in one unambiguous command with explicit exclusions —
   preserving numeric ownership and metadata — into a **new** directory, leaving the
   binhex tree untouched as the rollback artifact.
   - ⛔ **Exclude** `openvpn/` (⚠️ never copy `credentials.conf` into the new tree),
     `privoxy/`, `perms.txt`, `supervisord.log*`, `deluged.pid`.
   - Everything else comes across, including root-level plugin configuration
     (`label.conf`, `execute.conf`, `scheduler.conf`, …) that a whitelist would drop and
     that carries the label mappings.
4. Preserve the **exact in-container download paths** initially — Deluge stores absolute
   paths in state, and that variable is separable from the image.

⛔ **A clean re-add is the fallback, not the plan** — it forces a full hash check and
discards queue order, pause state, labels, priorities, ratios and seeding history.

### Image choice — DECIDED: upgrade in the same move, deliberately

No replacement image exists at Deluge 2.1.1; both `linuxserver/deluge` and binhex's
non-VPN `binhex/arch-deluge` start at **2.2.0**. An earlier draft proposed running the
current VPN image with `VPN_ENABLED=no` (supported — `init.sh:119`) to hold the version.
**Rejected by the owner, correctly:** it carries the VPN bundle forever to avoid a risk
this design already neutralises.

✅ **`binhex/arch-deluge`, digest-pinned to
`sha256:503ac5b44839bd2967ed4f4d9cf3349eda0d5fc2d7f51b360b49f0aaed3d1298`** — what both
`2.2.0-2-02` and `latest` resolve to today.

- **The rollback tree is never opened**, so a forward `core.conf` migration cannot make
  rollback one-way. That is what makes upgrading in the same move affordable.
- **Same image family** — the state was written by binhex's Deluge, so binhex→binhex
  changes only the version, not the conventions. LinuxServer would change family *and*
  version: the real two-variable problem.
- ⛔ **Default tag track only.** The `-libtorrentv1` variants are libtorrent 1.x and would
  invalidate all 47 torrents' resume data.
- ⛔ **Pin the digest, never the tag.**

### Compatibility gate — prove it offline, on a THIRD copy

⛔ v1 treated "2.1.1 + libtorrent 2.0.x" as a compatibility proof. It is not: resume
compatibility is directional and format-version dependent, and v1 ignored Python and
plugin compatibility entirely.

Before selecting the digest, start the candidate **offline against a third throwaway
copy** and prove: all 47 torrents present with matching infohashes, save paths intact,
queue and paused/active state preserved, plugins load, and **zero rechecks**. A mass hash
check costs hours of I/O and seeding ratio, and no rollback undoes it once started.

### ⛔ The other two groups need the same treatment

v1 designed state handling only for `deluge-books`. `deluge-vpn` and `qbittorrent-books`
also have config hostPaths, and without a stopped copy, identity baseline, compatibility
gate and rollback artifact each, the first start of a new image can rewrite the only state
tree they have.

---

## Cutover surface

### ⛔ AddOn filenames must sort AFTER the current bridge owners

k3s auto-deploy applies in filename order, and dropping a manifest never prunes. The
natural names are **unsafe**: `minas-deluge-*.yaml` sorts before `minas-docker-bridges.yaml`
(which owns `deluge-books`, `deluge-books-docker`, `deluge-vpn`, `deluge-vpn-docker`), and
`minas-qbittorrent-books.yaml` sorts before `minas-readmeabook.yaml` (which owns the
`gluetun` Service). The new Pods would come up healthy and then have their Services pruned
by the later-applying old owner.

✅ Use a **`minas-vpn-*.yaml`** prefix — sorts after both `minas-docker-bridges.yaml` and
`minas-readmeabook.yaml`. Record the reason next to the filenames so nobody "tidies" them.

### Services, endpoints, ingress

- Pin **all three** existing ClusterIPs, not only `deluge-books`.
- ⛔ Explicitly delete **every** stale EndpointSlice — `deluge-books-docker` **and**
  `deluge-vpn-docker` — or kube-proxy load-balances onto a dead Docker endpoint.
- Take over the **`gluetun` Service in `readmeabook.yaml`**, which readmeabook uses as its
  download client.
- All three hostnames exist **only as Docker labels** and vanish when Docker stops:
  `btbooks.saldivar.io`, `bt.saldivar.io`, `books-dl.saldivar.io`. Each needs a
  `traefik-routes.nix` backend (`hosts = [ … ]` style) in the cutover commit or public
  ingress 404s.
- ⛔ The PIA-forwarded **peer port arrives through `tun0`** — no `hostPort`, NodePort or
  ingress route.
- Update the Docker synthetic bridge probes in `monitoring.nix` in the **same** cutover, or
  they permanently report dead bridges.

---

## Acceptance — adversarial and continuous

⛔ `ip link set tun0 down` is **not** a fair test: OpenVPN is still alive and can recreate
the interface. ⛔ Nor is a rule snapshot: rules can be correct at both ends of a
transition that leaked in the middle.

Against the exact pinned digests, **generating traffic continuously throughout**:

1. Exit IP is a PIA address, not `99.64.240.101`, from inside the app container.
2. Four distinct failure modes, each with continuous new-flow attempts across **IPv4 and
   IPv6, TCP, UDP and DNS**: gluetun PID-1 termination; graceful sidecar restart; internal
   OpenVPN restart; endpoint blackholing. Nothing may egress in any of them.
3. **DNS specifically**: confirm the Pod resolves via **127.0.0.1** (gluetun), not
   `10.43.0.10`. With the tunnel down, name resolution must fail.
4. **Relay Pods on BOTH nodes** — minas *and* pelargir. ⚠️ The **local-node** relay is a
   KNOWN, ACCEPTED gap (see the residual-risk section) and is expected to succeed; record
   the result rather than treating it as a regression. The cross-node and Pod-to-Pod relay
   attempts must fail.
5. NetworkPolicy proven with a positive **and** negative test — ⛔ noting that a blocked
   connection surfaces here as **`Connection refused`, not a timeout** (measured; see open
   question 6). A step keyed on "timeout means blocked" will misread a working block.
6. gluetun control server unreachable from another Pod.
7. Ingress works for all three hostnames, same-node and cross-node; prowlarr and
   readmeabook still reach their download clients.
8. Pod recreation and a **node reboot**, re-running 2–4 afterwards.
9. Deluge: 47 torrents, matching infohashes, correct queue and **paused/active** state,
   plugins loaded, **no mass hash check**.

---

## Rollback — exact, because the vague version is unsafe

⛔ v1 said "remove the manifest from pelargir, rebuild, confirm no Pod remains". **That
does not roll back.** Removing an entry from `manifests.nix` neither deletes the
auto-deploy file nor the Kubernetes objects, so Docker would start while a live Pod still
manages the same torrent payloads — two writers on one state tree.

Per group, in this order:

1. Remove the entry from `manifests.nix` **and** delete the `minas-vpn-*.yaml` file from
   pelargir's auto-deploy directory; rebuild pelargir.
2. **Explicitly delete the Kubernetes objects** — Deployment, Services, EndpointSlices,
   NetworkPolicy — and confirm no Pod and no process remains.
3. Restore the bridge Service ownership to its original AddOn and confirm the ClusterIP
   and EndpointSlice are back.
4. Revert the `traefik-routes.nix` entries and `monitoring.nix` probes.
5. Only then `docker compose --profile migrated up -d <service>`.

⛔ Neutralise Kubernetes **first**, every time.

---

## Explicitly NOT changing

- The 20 already-migrated services. hostPath is correct here: the data exists only on
  minas and there is no HA. Rewriting working media services onto PVCs would add migration
  risk without adding availability.
- readmeabook and tracearr bundling app + Postgres + Redis — upstream-defined; decomposing
  them is an application rewrite with restore risk.
- `traefik-routes.nix` as the route source — a justified transitional exception while
  Docker Traefik is the host-owned edge. Routes become Kubernetes Ingress **after** traefik
  itself migrates.
- hostPorts generally. `flaresolverr:8191` and `tracearr:3069` look redundant and should go
  when those workloads are next touched, but the `*arr` hostPorts must not be removed
  without checking LAN clients and saved integrations.

## Open questions carried into build

1. ~~Does a Deluge image exist at 2.1.1?~~ **ANSWERED: no.** Upgrading to
   `binhex/arch-deluge` 2.2.0 deliberately; verify its `/config` layout and PUID/PGID
   handling match the copied tree.
2. MAM registration retry/rate-limit semantics — sets poll interval and backoff ceiling.
3. **PIA simultaneous-connection capacity — preflight three usable forwards before
   building**, since the three-Pod split depends on it.
4. Minimum capability set per container (table above) — measure, do not assume.
5. ~~Does Deluge 2.2.0 read 2.1.1's `torrents.state` without a rehash?~~ **ANSWERED
   2026-08-08: yes, PROVEN.** Ran `binhex/arch-deluge@sha256:503ac5b4…` (deluged **2.2.0**,
   libtorrent **2.0.11.0**, Python 3.14.2) against a throwaway copy with `--network none`
   so it could not announce, media mounted read-only:

   | check | baseline (2.1.1 / lt 2.0.10) | candidate (2.2.0 / lt 2.0.11) |
   |---|---|---|
   | torrents loaded | 47 | **47** |
   | unique infohashes | 47 | **47, zero differences** |
   | state distribution | 41 `[S]`, 6 `[D]` | **41 `[S]`, 6 `[D]`** |
   | at 100% | 41 | **41** |
   | hash recheck | — | **none** (the only "check" log line is `Checking GeoIP.dat`) |

   Baseline retained at `/root/deluge-books-baseline-20260808.txt` on minas; test container
   and copy destroyed.

   ⚠️ **Two things learned that affect the build:**
   - ⛔ **The Deluge container makes an OUTBOUND call at startup** — binhex's start-script
     does a geo lookup (`geo.el0.org`) and blocks in a bounded retry loop (~2 min) when it
     cannot resolve. It starts fine afterwards, but this means the container needs working
     DNS *from gluetun* before it becomes useful, which reinforces the `dnsPolicy: None` +
     `127.0.0.1` requirement and argues for a generous `startupProbe` `failureThreshold`.
   - ⚠️ **Do not read `du -sh` on `/storage2` as apparent size** — ZFS compression reported
     a faithful 3.0M copy as "37K". Compare with `du --apparent-size`, or a correct copy
     looks like a catastrophic one.

   ❓ Still to prove at cutover: this test used a **live** (crash-consistent) copy, so
   `torrents.fastresume` differed from source mid-write. The real migration uses a
   **stopped** copy, which is strictly better — but re-verify counts and states after it.
6. ~~Is NetworkPolicy genuinely enforced on this cluster?~~ **ANSWERED 2026-08-08: yes,
   verified.** Two throwaway Pods on minas in a `netpol-test` namespace: no policy →
   request succeeded; default-deny ingress applied → blocked; policy deleted → succeeded
   again. The server stayed `Running` and reachable on its own loopback throughout, so a
   dead listener is ruled out as the cause. Namespace deleted.

   ⚠️ **Calibration for the acceptance tests: a blocked connection here surfaces as
   `Connection refused`, NOT as a timeout.** k3s's policy controller rejects rather than
   drops. Any acceptance step that treats "timeout" as the signal for "blocked" will
   misread a working block. This belongs with the other false-answer probes in
   `K3S-HANDOFF.md`.
