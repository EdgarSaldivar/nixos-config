# Default-deny NetworkPolicies — DRAFT, NOT DELIVERED

**Status: written, reviewed by nobody, applied nowhere.** These are deliberately
outside `hosts/nixos/*/manifests/`, so `pelargir/manifests.nix` cannot pick them
up and no `nixos-rebuild` can apply them by accident. They enter the catalog only
after the validation below passes against the live cluster.

## Why they are not delivered

`books`, `media`, `games`, `nextcloud` and `immich` have no default-deny policy.
That was deferred "until Phase 6" because traefik was then an external Docker
container and a namespace-scoped policy could not have admitted it. Traefik is now
an in-cluster Pod, so the reason is gone — but the risk is not.

A wrong NetworkPolicy does not fail loudly. It drops traffic, and in this fleet the
symptom is *"slow torrents"* or *"Overseerr can't reach Sonarr"* — read as an
application fault, days after the change. `media` alone has 22 Services that talk
to each other by bare name. Getting this wrong is cheap; discovering it is not.

So: written to be read, argued with, and applied by hand one namespace at a time.

## What these assume, and what must be checked

Each assumption below is a way these can be wrong. None is verified.

1. **Traefik reaches every namespace it routes to.** Modelled by admitting ingress
   from `namespaceSelector: kubernetes.io/metadata.name: traefik`. Requires that
   label to exist on the traefik namespace — k3s sets `kubernetes.io/metadata.name`
   automatically on modern versions, but **verify it** rather than assume.
2. **Intra-namespace traffic is unrestricted.** `media`'s services reference each
   other by bare name constantly; enumerating those edges would be a large, fragile
   allow-list. These policies allow all same-namespace traffic and restrict only
   what crosses a boundary. That is a deliberate weakening — it buys correctness
   over precision, and it is still strictly better than nothing.
3. **`books` needs `media`.** `readmeabook` reaches `prowlarr` through an
   ExternalName CNAME into `media`. That edge is explicitly allowed; if any other
   cross-namespace edge exists it is **not** in these drafts and will break.
4. **Egress to the internet is required** by the `*arr` stack, deluge, immich's
   map tiles, and nextcloud. These policies do not restrict egress at all except
   for DNS, which must be allowed explicitly once any egress rule exists.
5. **`authentik` already has its own policies** and is untouched here. Its
   ForwardAuth path is traefik → authentik, not namespace → authentik, so it is
   unaffected.
6. **The VPN-gated Pods run with `dnsPolicy: None`** and their own DNS. A
   namespace policy that assumes cluster DNS for every Pod would be wrong for
   them; these do not add egress restrictions, so they are unaffected — but that
   is why egress is left open rather than tightened.

## Validation, before any of this is delivered

Per namespace, in this order, starting with the least-connected (`games`, then
`immich`, `nextcloud`, `books`, and `media` last):

```sh
# 1. Apply by hand, to ONE namespace.
kubectl apply -f experiments/network-policies/<ns>.yaml

# 2. Prove the label the policy depends on actually exists.
kubectl get ns traefik -o jsonpath='{.metadata.labels}' ; echo

# 3. Exercise the ingress path from OUTSIDE, not from inside the cluster.
#    A curl from a Pod proves nothing about traefik's path.
python3 hosts/nixos/minas-tirith/scripts/ingress-acceptance.py \
  --baseline hosts/nixos/minas-tirith/baselines/minas-ingress-authentik-baseline-*.txt

# 4. Exercise the intra-namespace path that matters for that namespace:
#    media      — Sonarr -> Prowlarr, Sonarr -> deluge-vpn
#    books      — readmeabook -> prowlarr (CROSS-namespace, the risky one)
#    nextcloud  — app -> nextcloud-db
#    immich     — app -> immich-postgres14 and immich-redis

# 5. Watch for at least one full *arr sync cycle before moving to the next
#    namespace. The failure mode is delayed, not immediate.
```

⛔ **Rollback is `kubectl delete -f <file>`** and is immediate. That is only true
while these live here — once they are in the auto-deploy catalog, removing the file
does NOT remove the policy, because k3s never prunes. Deliver them only after all
five have been validated, and deliver them as one commit so the ownership map
records them together.

## When they graduate

Move the files under `hosts/nixos/minas-tirith/manifests/`, add them to
`pelargir/manifests.nix` with `minas-netpol-` prefixed basenames (frozen from that
moment), and delete this directory. `checks/manifest-objects.nix` will assert their
identities do not collide with anything existing.
