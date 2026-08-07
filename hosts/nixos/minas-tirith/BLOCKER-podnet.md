# ⛔ BLOCKER — cross-node pod networking is down (found 2026-08-06)

**Nothing can migrate to k3s until this is fixed.** It requires a Tailscale admin
action; it cannot be fixed from the hosts.

## Symptom

A pod on `minas-tirith` cannot reach anything on `pelargir`:

```
minas pod 10.42.1.3  ->  CoreDNS pod 10.42.0.5      100% packet loss
minas pod            ->  kube-dns svc 10.43.0.10    unreachable
nslookup kubernetes.default                          connection timed out
```

`/etc/resolv.conf` in the pod is correct (`nameserver 10.43.0.10`). The problem is
purely routing.

## Why it was invisible

Both nodes have shown `Ready` since minas joined, and every check to date passed —
because **minas has been running zero k8s workloads**. Nothing exercised the
cross-node path. `MINAS-PREP.md` §4 explicitly calls for "the cross-node pod test";
that test was never run, and this is exactly what it exists to catch.

## Root cause

k3s here runs **flannel with the `tailscale` backend** (there is no `flannel.1`
interface, only `tailscale0`), so inter-node pod traffic depends on each node
advertising its pod CIDR as a **tailnet subnet route**.

Both nodes do their half correctly:

| | AdvertiseRoutes | RouteAll (accept) |
|---|---|---|
| pelargir | `10.0.1.0/24`, `10.42.0.0/24` | `true` |
| minas-tirith | `10.42.1.0/24` | `true` |

But the routes are **not approved in the tailnet**, so nothing is installed:

```
minas    # ip route show 10.42.0.0/24   -> (nothing)
pelargir # ip route show 10.42.1.0/24   -> (nothing)
minas    # ip route show dev tailscale0 -> (nothing)
```

With no route, pod traffic falls back to the default gateway (`via 10.0.0.1 dev eth0`),
where `10.42.x` is not routable, and is dropped.

## Fix — requires the tailnet admin

**Option A (durable, preferred).** Add an ACL auto-approver so any fleet node's pod
CIDR is approved on join — this also covers osgiliath and any future node:

```json
"autoApprovers": {
  "routes": {
    "10.42.0.0/16": ["tag:fleet"]
  }
}
```

Both nodes already carry `tag:fleet`.

**Option B (manual, per node).** Tailscale admin console → Machines → each node →
*Edit route settings* → approve `10.42.0.0/24` (pelargir) and `10.42.1.0/24` (minas).

## Verify after fixing

```bash
# routes installed
ssh minas    ip route show 10.42.0.0/24     # expect: via tailscale0
ssh pelargir ip route show 10.42.1.0/24

# the real test — a pod on minas resolving cluster DNS
k3s kubectl create namespace nettest
k3s kubectl -n nettest run probe --image=busybox:1.36 --command -- sleep 600
k3s kubectl -n nettest exec probe -- nslookup kubernetes.default.svc.cluster.local
k3s kubectl delete namespace nettest
```

Only when that resolves is the cluster ready for a workload to move.

## Note for the plan

This belongs in Phase 0 as a prerequisite nobody had written down: **prove cross-node
pod networking before anything migrates.** It is not in D9 (service discovery), which
assumed the network worked and dealt only with name resolution between services.
