# minas-tirith — ingress maintenance

traefik is the live public ingress for 26 hostnames, as a **singleton k3s Pod with
`strategy: Recreate`**. Any change to `spec.template` in `manifests/traefik.yaml`
is a full ingress outage, not a rolling update.

⛔ Run `kubectl diff -f <candidate>` before any edit. **Empty output is the proof**
there is no template change.

Salvaged from TRAEFIK-CUTOVER-RUNBOOK.md on 2026-08-16; the cutover itself completed
2026-08-10 and its narrative is in git history behind the pre-doc-cleanup-2026-08 tag.

## The canary manifests


Two canary Pods ran on 2026-08-09 while docker kept serving production, then were
deleted. Both live in `experiments/traefik-canary/`, outside the manifest tree, so
their exclusion from auto-deploy is structural rather than something a reader must
infer. They are applied with `kubectl apply -f` and deleted afterwards.

⚠️ An earlier version of this paragraph said they were committed at
`manifests/traefik-canary.yaml` and `manifests/traefik-canary-b.yaml` and then, two
sentences later, that they live in `experiments/`. Only the second was true after
the move; the first named paths that no longer exist.

⛔ **Render the canary, do not apply the file directly.** Its Cloudflare
`trustedIPs` list is a placeholder, substituted from
`hosts/nixos/pelargir/cloudflare-ranges.nix` — the same expression that renders
production:

```sh
nix build .#traefik-canary
sudo k3s kubectl apply -f result
```

Until 2026-08-16 the canary carried its own hand-copied range list and was the
FOURTH copy of those ranges. A canary exists to behave like production; a canary
with its own drifting configuration tests something production is not doing.

## Rollback order, if the edge must go back

⚠️ **The obvious order is wrong.** Scaling imperatively to 0 while the installed
manifest still says `1` leaves a **resurrection window**: any k3s reapply, pelargir
activation or server restart starts the Pod again, giving competing hostPort rules
and two writers on `acme.json`. The declaration goes first.


1. Set `manifests/traefik.yaml` back to `replicas: 0` (revert the promotion commit or make
   an explicit rollback commit).
2. `rsync` and rebuild **pelargir**, so the installed manifest reads 0. That apply performs
   the scale-down.
3. Verify **three** things: installed file is 0, Deployment is 0, and the `minas-traefik`
   AddOn reports a **successful apply** — see the gate note below.
4. Wait for Pod deletion, then prove no traefik container task remains **via the CRI**.
5. Preserve the suspect `acme.json`, restore the known-good copy atomically.
6. ⛔ **This step is dead.** It read `docker start e230f30a9d3f` — restarting the
   retained pre-cutover traefik container. Docker was decommissioned on minas on
   2026-08-17: no daemon, no containers, no images. There is no docker rollback.

   The rollback is the k3s one above: restore `acme.json` and bring the Deployment
   back up. If that cannot be made to work, the honest position is that this ingress
   has no second path and restoring it is a rebuild, not a start.
7. Re-check that both the delivered manifest and the Deployment still read 0.

⛔ From step 1 until step 3 is verified, no concurrent pelargir activation or k3s restart.
If an emergency forces imperative scaling first, auto-deploy must be **positively
neutralised**, not assumed idle.

⚠️ **Every `acme.json` snapshot has a shelf life.** A copy is coherent only while
the store is unchanged. After a renewal (first one due ~Sep 16 minus 30 days) restoring it
would **discard newer certificates and account state**. Re-validate before any later
rollback.

⚠️ **Step 6 is now obsolete.** It said `docker start <id>` — restarting the retained
Compose container. Docker is at zero containers and its decommission is held only
until roughly 2026-09-09. After that date this rollback has no docker fallback and
the only path back is a working k3s edge.
