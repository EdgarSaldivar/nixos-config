# The k3s cluster

One cluster spanning two houses, joined over Tailscale. `pelargir` is the sole
server; `minas-tirith` and (when deployed) `osgiliath` are agents.

Manifests live per host under `hosts/nixos/<host>/manifests/`, but **every one of
them is delivered by pelargir** from its auto-deploy directory, because agents do
not have one. The directory layout describes ownership; it does not describe
delivery.

---

## ⛔ Durable state belongs in git

**This is the rule the media manifests cite.** Never `kubectl scale` a workload
and leave its manifest disagreeing.

k3s auto-deploy re-applies a manifest when its **file checksum changes** *or* when
the **server restarts**. So an imperative `kubectl scale --replicas=1` survives
only until the next edit to that file or the next k3s restart — at which point the
declared value is silently reasserted and the service goes down, with nothing in
git to explain why.

This is also why hand-scaling *appears* to survive an ordinary `nixos-rebuild`:
`install` of byte-identical content does not change the checksum, so nothing
re-applies. That property is what hid the problem for weeks.

For a CronJob the same mistake is worse than an outage because it is **silent**: a
reverted `suspend: true` simply stops taking backups, and marker staleness would
not report it for two days.

> Replica counts in manifests are authoritative desired state; do not infer them
> from an imperative cluster observation.
>
> Osgiliath's four workloads deliberately declare `replicas: 0` until commissioning
> prerequisites are satisfied: the node is registered, its required paths and
> devices are available, and each migration gate is ready. Pelargir delivers these
> manifests and each Pod pins `nodeSelector: kubernetes.io/hostname: osgiliath`, so
> raising them earlier creates Pending or init-blocked Pods. Raise all four in a
> commit, never with `kubectl scale`.

## ⛔ Never label minas with `svccontroller.k3s.cattle.io/enablelb=true`

k3s ServiceLB is confined to pelargir by that label. Adding it to minas puts k3s
into a fight for ports 80/443 with the traefik Pod that owns them via `hostPort`.

## Tailnet transport, and the route approval that is invisible when missing

Cluster traffic runs over Tailscale. Pod-to-pod traffic across nodes depends on
the advertised **pod-CIDR subnet routes being approved in the tailnet**. When they
are not, cross-node pod networking fails in a way that looks like nothing at all:
Services resolve, Pods are Ready, and connections simply do not complete.

This cost about six hours before it was found, and **it will recur for any new node
whose routes are not auto-approved** — so it is the first thing to check when a
newly joined node's Pods cannot reach anything.

⚠️ One diagnostic correction worth keeping, because the wrong version is
convincing: "there are no tailscale-installed routes at all" is **not** evidence.
Tailscale installs its routes in **table 52**, so they are invisible to a plain
`ip route` and the absence proves nothing.

## VPN-gated workloads

The VPN-gated groups (`deluge-books`, `deluge-vpn`, and the
gluetun/qbittorrent/flaresolverr netns trio) were **designed for Kubernetes, not
ported** from their Compose originals.

- Containers that shared a container's network namespace under docker become
  **one multi-container Pod**, not separate Deployments.
- PIA credentials reach gluetun as a **Secret volume** through its `*_SECRETFILE`
  variables, never as `secretKeyRef` env. A resolved env value lands in
  containerd's on-disk container metadata on the node, which defeats the point of
  encrypting it.
- Neither VPN manifest declares its own Service. Both selector-backed Services
  remain in `manifests/docker-bridges.yaml` under their original AddOn owner.
  Moving one to its workload's manifest would be a
  delete-and-recreate across two AddOns: the AddOn applied later prunes what the
  other just created, leaving a window with no Service and a new ClusterIP.

That last point is why `docker-bridges.yaml` still has a misleading name. Renaming
it would be the exact delete-and-recreate the arrangement exists to avoid.

## Manifest delivery is not transactional

`pelargir/manifests.nix` rewrites every auto-deploy file on activation, through
the single server. A missing, renamed or mis-pathed entry is **not** rolled back by
`nixos-rebuild --rollback`, because Nix rollback does not undo objects k3s has
already applied.

⛔ **Basenames are frozen.** The AddOn identity is derived from the installed
filename. Renaming an entry creates a NEW AddOn and leaves the old one owning its
objects forever, because k3s never prunes a file that disappears. Deleting that
orphaned AddOn to tidy up can garbage-collect a live Namespace and everything in
it.

⚠️ Expect one `ApplyManifestFailed` warning per new namespace on first apply. k3s
applies the directory in **filename order**, so a workload can be applied before
its namespace exists; k3s retries ~15s later and succeeds. That is ordering, not
breakage, and it is deliberately not "fixed" by renaming files to sort earlier.
