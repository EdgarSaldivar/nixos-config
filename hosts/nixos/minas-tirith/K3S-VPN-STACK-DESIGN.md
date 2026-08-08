# VPN-gated stack on k3s — design, not a port

**Status: DESIGN, nothing built.** Supersedes the "translate binhex faithfully" approach
for the three VPN-gated groups. Written 2026-08-08 after the owner redirected the work:
*"move away from direct migration/preservation to how IT SHOULD be done… the goal is a
port yes but also an improvement, not just a port for porting sake."* Breakage is to be
avoided but is not fatal — fix forward.

Read `K3S-HANDOFF.md` first for fleet conventions, the deploy discipline, and the traps.

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

The improvement is not stylistic. Three concrete defects go away: the privileged
container, the credential file on disk, and a hand-rolled kill-switch we would own
forever. `qbittorrent-books` and `flaresolverr-books` already run as
`NetworkMode=container:<gluetun>`, which is exactly a shared Pod netns — so this pattern
is already load-bearing here, just under Docker.

⚠️ gluetun today sets `OPENVPN_USER`/`OPENVPN_PASSWORD` as **env**; the `*_SECRETFILE`
variables are at their defaults and unused. The file-based path is available and simply
has not been adopted. Adopting it is the single change that makes the secret goal real.

---

## Topology — three groups, not one Pod

⛔ **Do not build one mega-Pod.** PIA's port-forward assigns **one port per connection**,
and all containers in a Pod share one address and port space. Two torrent clients cannot
both bind the same peer port, and one tunnel cannot supply two advertised ports. There are
**three** torrent clients here, not two.

| Pod | containers | notes |
|---|---|---|
| `deluge-books` | gluetun sidecar + deluge + **MAM registrar** sidecar | MAM is books-only |
| `deluge-vpn` | gluetun sidecar + deluge | its own tunnel and forwarded port |
| `qbittorrent-books` | gluetun sidecar + qbittorrent + flaresolverr-books | preserves an already-working shared-egress group |

flaresolverr needs no inbound peer port, so sharing is safe there. The Pod-count saving
from further merging is not worth coupled upgrades, coupled restarts and a wider VPN
blast radius.

---

## The gluetun sidecar — and the correction that matters

gluetun runs as a **native sidecar**: an `initContainer` with `restartPolicy: Always`.
k3s is v1.35.6, so this is GA.

⛔ **A native sidecar alone does NOT guarantee the firewall is up before the app starts.**
Kubernetes proceeds to the next container once a restartable init container is merely
*running* — unless it has a **passing `startupProbe`**. Without one, "gluetun starts
first" is not "gluetun is ready first", and there is a real egress window. gluetun
therefore gets an exec `startupProbe` bound to its own healthcheck, and a readiness probe
that contributes to Pod readiness.

Security context, stated precisely:

- gluetun: `drop: [ALL]`, then `add: [NET_ADMIN]`. Test whether ICMP health checks pull in
  a `NET_RAW` dependency; add it only if proven necessary.
- app containers: `drop: [ALL]`, `allowPrivilegeEscalation: false`, `RuntimeDefault`
  seccomp, **no** capabilities. They simply use the Pod netns.
- ⛔ Omitting `capabilities.add` does **not** mean "no capabilities" — the runtime's
  default set remains. `drop: [ALL]` is the only thing that empties it.
- `/dev/net/tun` as a `hostPath` `type: CharDevice` (present on the node as
  `crw-rw-rw- 10,200`).
- `automountServiceAccountToken: false`; every image **digest-pinned**.

### Restart semantics — why this is fail-closed, and where it is not

Containers in a Pod share the netns; the sandbox outlives any individual container.
Netfilter rules are **namespace** state, not process state. So if gluetun crashes while
the sandbox lives, its DROP rules remain and the vanished tunnel leaves the apps
fail-closed. If the sandbox is replaced, the app containers go with it and gluetun starts
first in the new one.

⚠️ This is not a zero-window proof and must not be written up as one. Readiness going
false does not stop already-running processes or established connections — **the firewall
is the safety boundary, not the probe.** That is why the acceptance test below is
adversarial rather than confirmatory.

### Firewall scope — a real trade, made deliberately

gluetun today uses `FIREWALL_OUTBOUND_SUBNETS=172.16.0.0/12,192.168.0.0/16` — already
tighter than binhex's `LAN_NETWORK=10.0.0.0/8`. On k3s the Pod needs cluster DNS and
Service reachability, so the cluster ranges (`10.42.0.0/16` pods, `10.43.0.0/16` services)
must be allowed outside the tunnel.

⛔ **Do not port the broad `10/8` allowance.** It would permit escape via another Pod or a
node process. Allow the two cluster CIDRs and nothing more, and state plainly that this is
*narrower than today but not zero* — a VPN-gated Pod on Kubernetes cannot function with no
non-tunnel egress at all.

---

## Secrets — what is actually being promised

Delivery: sops → rendered to tmpfs on pelargir → applied by `k3s-apply-secrets` → mounted
as a **Secret volume** → consumed via gluetun's `*_SECRETFILE` variables.

- ⛔ **Not `secretKeyRef`.** Resolved env values reach containerd's on-disk container
  metadata, which defeats the point. Secret volumes are tmpfs-backed.
- ⛔ **Mount the whole Secret directory — never `subPath`.** subPath mounts do not receive
  updates, so a rotation would appear to succeed and change nothing.
- Repo-wide, `secretKeyRef` is used only by `tracearr.yaml` and `palworld.yaml`. Convert
  both when those workloads are next touched — not as a separate campaign.

### State the guarantee honestly

Secret **volumes stop the injected copy from reaching node storage. They do not stop an
application from persisting credentials into its own hostPath config, logs or state**, and
private-tracker `.torrent` files carry announce tokens regardless. "No plaintext
credential anywhere on disk" is **not achievable** by changing secret delivery; it needs
encrypted datasets (`storage2` is `encryption=off`, and ZFS encryption is creation-time
only).

✅ The achievable, verifiable guarantee is: **deployment secrets are never materialized
into persistent runtime metadata or manifests.** Claim that, prove that, and track
storage encryption as separate work.

### Rotation is one transaction

⛔ `k3s-apply-secrets` does not react to value-only sops rotations, and applying a changed
Secret does **not** restart gluetun, which reads credentials at startup. A rotation that
stops halfway leaves the old credential live while looking done. The transaction is:
apply Secret → confirm resourceVersion changed → recreate affected Pods **one at a time**
→ prove tunnel up and exit IP non-local between each.

---

## The MAM registrar — a sidecar, not a hook

MAM sessions are **IP-bound**. Today a literal `mam_id=` cookie in argv calls
`/json/dynamicSeedbox.php` after the tunnel comes up. Dropping it breaks MAM **not at
cutover but at the next exit-IP change** — silently, and looking unrelated.

⛔ **Do not use gluetun's port-forward up/down hook for this.** It fires on "port
forwarding established", not "exit IP changed", so registration would stop silently if
forwarding failed while egress stayed usable. Use the port-forward hook for its actual
job: updating each torrent client's listening port.

The registrar is a small sidecar that:

- mounts **only** the MAM Secret;
- watches gluetun's `PUBLICIP_FILE` (`/tmp/gluetun/ip`) via a shared **memory-backed**
  `emptyDir`, and registers on every observed change, with bounded backoff;
- has a `startupProbe` that succeeds only after the first successful registration, so
  Deluge cannot start before MAM is usable;
- performs the HTTP **itself**, so the cookie never appears in a child argv or a log.

❓ MAM's retry/rate-limit behaviour is unknown and must be measured before fixing the poll
interval.

---

## Deluge state — preserve it, and never let the new image touch the rollback

Measured in the live container: **Deluge 2.1.1, libtorrent 2.0.10.0**, Python 3.13.1,
**47 torrents**, `state/torrents.state` present, tree owned `1000:1000` (so LinuxServer
`PUID/PGID=1000` maps directly).

⛔ **A clean re-add is the fallback, not the plan.** It forces a full hash check of every
torrent and discards queue order, pause state, labels, file priorities, ratios and seeding
history even though the payload survives.

Procedure:

1. Pause everything; record torrent count, infohashes, save paths, queue state.
2. Stop the container. Copy the **complete** stopped tree to a **new** host directory with
   numeric ownership and metadata preserved (`cp -a`), leaving the binhex tree untouched
   as the rollback artifact.
3. Copy: `core.conf`, `web.conf`, `auth`, `hostlist.conf`, `session.state`, `icons/`,
   `ssl/`, `plugins/`, and the **entire** `state/` tree — `torrents.state`, fast-resume
   data and all 47 `.torrent` files. Copying only `core.conf` + `.torrent` files loses
   everything in the paragraph above.
4. **Exclude** the binhex-specific artifacts: `openvpn/`, `privoxy/`, `perms.txt`,
   `supervisord.log*`, `deluged.pid`.
5. Preserve the **exact in-container download paths** initially. Deluge stores absolute
   paths in state; changing them and the orchestrator at once makes a failure ambiguous.

### Image choice — DECIDED: upgrade in the same move, deliberately

Measured against the registry: **no replacement image exists at Deluge 2.1.1.** Both
`linuxserver/deluge` and binhex's own non-VPN `binhex/arch-deluge` start at **2.2.0**.

An earlier draft proposed keeping the running `arch-delugevpn` image with `VPN_ENABLED=no`
(supported — `init.sh:119` branches on it) to hold the application at 2.1.1 and upgrade
later. **Rejected by the owner, correctly:** it carries the VPN bundle forever to avoid a
risk that this design already neutralises.

✅ **`binhex/arch-deluge`, digest-pinned to
`sha256:503ac5b44839bd2967ed4f4d9cf3349eda0d5fc2d7f51b360b49f0aaed3d1298`** — which is
what both `2.2.0-2-02` and `latest` resolve to today.

Why this is safe rather than reckless:

- **The rollback tree is never opened.** State is copied to a *new* directory and the
  binhex tree is left untouched, so 2.2.0 migrating `core.conf` forward cannot make the
  rollback one-way. That protection is what makes upgrading in the same move affordable.
- **The real compatibility boundary is libtorrent, not Deluge.** Fast-resume data is
  libtorrent's format, and both images are libtorrent **2.x**. ⛔ Take the DEFAULT tag
  track — the `-libtorrentv1` variants are libtorrent 1.x and **would** invalidate all 47
  torrents' resume data.
- **Same image family.** The state tree was written by binhex's Deluge; staying
  binhex→binhex changes only the version and drops the VPN bundle. LinuxServer would
  change image family *and* version at once — that is the genuine two-variable problem,
  not the version bump by itself.
- ⛔ **Pin the digest, never the tag.** `latest` is mutable; on a torrent client a silent
  re-pull could change libtorrent underneath live resume data.

⛔ Still do **not** change download paths in the same move. Deluge stores absolute paths in
state, and that variable is separable from the image — keep it fixed.

---

## Networking and ingress

- Expose only Web/API ports through **ClusterIP Services**.
- ⛔ The PIA-forwarded **peer port arrives through `tun0`** and needs no `hostPort`,
  NodePort or ingress route. Do not translate it into one.
- ⛔ **Do not expose gluetun's control API** (`:8000`).
- `btbooks.saldivar.io` exists **only as a Docker label today**. Stopping Docker deletes
  the router. A `traefik-routes.nix` backend for the new Service is required in the
  cutover commit or public ingress 404s.
- The bridge `deluge-books` Service in `docker-bridges.yaml` is consumed by prowlarr.
  Replacing it is a delete-and-recreate across two AddOns; pin the existing ClusterIP and
  **explicitly delete the `deluge-books-docker` EndpointSlice**, or kube-proxy will
  advertise the dead Docker endpoint alongside the Pod.

---

## Acceptance — adversarial, because confirmatory proves nothing

⛔ `ip link set tun0 down` is **not** a fair test: OpenVPN is still alive and can recreate
the interface before the probe runs. Against the pinned gluetun digest, prove:

1. Exit IP is a PIA address, not `99.64.240.101`, from inside the app container.
2. **Kill the gluetun/OpenVPN process** (and separately, blackhole its endpoint IPs) and
   confirm new TCP **and** UDP **and** DNS flows fail — continuously attempted, not
   sampled once.
3. **IPv6 fail-closed.** Attempt IPv6 TCP/UDP/DNS egress with the tunnel down. Today Pods
   have link-local IPv6 only; a later global route must not become a bypass.
4. **Same-node adversarial Pod**: attempt to relay traffic via another Pod on minas and
   via the node itself. Probing only public `1.1.1.1` will pass while a relay path exists.
5. Cluster DNS and Service access still work; ingress works same-node and cross-node.
6. Rule and route snapshots **before, during and after** a gluetun restart, plus a Pod
   recreation and a node reboot.
7. Deluge: all 47 torrents present, correct queue/pause state, **no mass hash check**.

---

## Rollback

Docker's volumes and the binhex `/config` tree are untouched, and `deluge-books` still
exists in compose (profiled out). Rollback is: remove the manifest from pelargir, rebuild,
confirm no Pod remains, then `docker compose --profile migrated up -d deluge-books`.
⛔ Neutralise Kubernetes **first** — two Deluge processes on one state tree is how the
torrent state is lost.

---

## Explicitly NOT changing

- The 20 already-migrated services. hostPath is correct for this fleet: the data exists
  only on minas and there is no HA. Rewriting working media services onto PVCs would add
  migration risk without adding availability.
- readmeabook and tracearr bundling app + Postgres + Redis. Inelegant but upstream-defined;
  decomposing them is an application rewrite with restore risk.
- `traefik-routes.nix` as the route source — a justified transitional exception while
  Docker Traefik is still the host-owned edge. Application routes should become Kubernetes
  Ingress objects **after** traefik itself migrates, not before.
- hostPorts generally. `flaresolverr:8191` and `tracearr:3069` look redundant behind
  internal Services and should be dropped when those workloads are next touched, but the
  `*arr` hostPorts must not be removed without checking LAN clients and saved integrations.

## Open questions carried into build

1. ~~Does a Deluge image exist at 2.1.1 + libtorrent 2.0.x?~~ **ANSWERED: no.** Decided to
   upgrade to `binhex/arch-deluge` 2.2.0 in the same move — see the image section above.
   Verify at build that its `/config` layout and PUID/PGID handling match the tree being
   copied (same author, so expected, but confirm rather than assume).
2. MAM registration retry/rate-limit semantics — sets the poll interval.
3. PIA simultaneous-connection limit — three gluetun tunnels plus any existing use.
4. Does gluetun need `NET_RAW` for ICMP health checks under `drop: [ALL]`?
5. Does Deluge 2.2.0 read 2.1.1's `torrents.state` without a rehash? Prove on the COPY
   before the cutover — this is now the one place the version bump could still cost hours
   of disk I/O and seeding ratio, and it is cheap to test in advance.
