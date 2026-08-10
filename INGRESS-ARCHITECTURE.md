# Ingress architecture — why there are TWO traefiks, on purpose

**Decision, 2026-08-09: KEEP BOTH.** They are two *site-edge gateways*, not accidental
duplication and not a failover pair. This document exists so nobody — including a future
session of an assistant — "cleans up the duplicate".

Reviewed independently before being written down. The reviewer reached the same verdict and
supplied most of the reasoning below.

---

## The physical arrangement is the whole argument

| | **pelargir** | **minas-tirith** |
|---|---|---|
| what | Raspberry Pi 5 | large x86 server |
| where | **the owner's house** | **a friend's house** |
| k3s role | **server** (control plane) | agent |
| workloads | Home Assistant, Zigbee2MQTT, Mosquitto, Frigate | ~30: Plex, Jellyfin, the *arr stack, nextcloud (1.5 TB), immich, VPN/torrent |
| why immovable | ⛔ **the Zigbee USB radio is physically plugged into this Pi** | GPU + ~60 TB of storage + the public port-forwards |

Two houses, two uplinks, two public IPs, one cluster joined over **Tailscale** (`wg0` is
pelargir's out-of-band lifeline, *not* cluster transport).

**Each traefik terminates traffic in the same house as the workloads it serves.** That is
the entire justification, and it is a property of the buildings, not of the software.

## The two edges

| | **minas edge** (`traefik` ns) | **pelargir edge** (`kube-system`) |
|---|---|---|
| image | `traefik` **3.7.10**, digest-pinned by us | `rancher/mirrored-library-traefik:**3.7.4**`, pinned by the k3s release |
| exposure | `hostPort` 80/443, direct port-forward | LoadBalancer + k3s ServiceLB, Cloudflare-fronted |
| routes | **~24 generated + hand-maintained**, file provider from `traefik-routes.nix` | **1** Ingress (Home Assistant), Ingress CRD |
| entrypoints | `http` / `https` | `web` / `websecure` |
| TLS | traefik's own ACME DNS-01, wildcard in `acme.json` | Secret `pelargir-wildcard-tls`, **issued by cert-manager** via ACME DNS-01 |

⚠️ **Both sites run ACME DNS-01 against the same Cloudflare zone.** An earlier note in this
repo claimed pelargir "does no ACME" — that is wrong at the fleet level: traefik there is not
the ACME client, **cert-manager is**.

⛔ **They share ONE Cloudflare API token** (verified identical by hash, 2026-08-09), with
whole-zone edit rights. Rotating or revoking it stops certificate renewal **at both houses**.
Accepted by the owner as a deliberate trade for now; the reviewer's recommendation was
separate least-privilege tokens per site, and that remains the better end state.

## Why not consolidate

- **Onto minas** — Home Assistant's ingress would depend on the friend's uplink and the
  inter-site tunnel. It saves one small Pod and enlarges HA's outage set. Rejected.
- **Onto k3s' bundled traefik** — all media traffic would cross the tunnel and fail with
  either pelargir or the tunnel. Doing it *properly* means two replicas with
  `externalTrafficPolicy: Local` and ServiceLB on both nodes — still two traefik processes,
  now sharing one version, route table and certificate blast radius. Viable later; not a
  cleanup.
- ⛔ **And it would cost the migration mechanism.** Generated routers carry **`priority: 1`**,
  the lowest, so a live `@docker` router always wins until its container stops. That is a
  *same-traefik, cross-provider* precedence trick, and it is what let every migration this
  year install a route **before** its workload existed, safely, one service at a time. A
  separate controller cannot see or lose to docker routers, so the final switch would become
  fleet-wide instead of incremental.

## Standing invariants — break these and something breaks

- ⛔ **Never label minas `svccontroller.k3s.cattle.io/enablelb=true`.** k3s ServiceLB is
  confined to pelargir by that label; adding it to minas puts k3s in a fight for ports 80/443.
- ⛔ **The minas edge is a singleton.** Host ports and `acme.json` are single-writer.
- ⛔ **Workload first, route second** — and they are delivered by *different hosts*
  (`manifests.nix` by pelargir, `traefik-routes.nix` by minas).
- ⚠️ **Two config models.** Copying config between edges mechanically can silently drop
  authentication or reference an entrypoint that does not exist (`websecure` is not defined
  on the minas edge; `traefik.yml` in its dynamic directory holds *static* keys that traefik
  silently ignores).
- ⚠️ **Two version owners.** A k3s upgrade can move 3.7.4 and its chart/CRDs without touching
  ours; a digest bump here does nothing to pelargir. Known drift is fine; unknown drift is not.

## The bigger risk, which is not the traefiks

⛔ **pelargir is a cluster-wide control-plane single point of failure.** One SQLite control
plane, in one house. If the Pi is down, minas' data plane may keep serving best-effort but
there is no API, no reconciliation, no cert-manager, no reliable restart or reschedule.

⚠️ Adding minas as a second control-plane member does **not** fix this — a two-member quorum
is not fault-tolerant. If minas must survive a long pelargir outage, the honest architecture
is **one cluster per house**, which costs a second control plane and duplicated operations
but matches the physical failure domains, and loses little scheduling flexibility because the
workloads are already immovable.

**This is an open decision, not a settled one.** It is recorded here because it is larger
than the ingress question and should be decided deliberately rather than discovered during
an outage.

## Failure modes

| failure | pelargir / HA | minas / the public hostnames |
|---|---|---|
| tunnel drops | stays up on its local edge | keeps serving, but the agent loses its control plane: no deploys, no endpoint updates, no rescheduling; DNS may be intermittent |
| pelargir down | fully down | data plane best-effort only; no API, cert-manager, or restart path |
| minas down | unaffected | ingress and workloads down together — no failover target exists, storage and GPU are pinned there |
| wildcard renewal fails | unaffected | existing cert serves until expiry, then TLS fails for every hostname |
| **shared Cloudflare token revoked** | **renewal stops** | **renewal stops** | ← the one true cross-house coupling |

Monitor each edge separately: its own public probe, its own certificate-expiry alert.
