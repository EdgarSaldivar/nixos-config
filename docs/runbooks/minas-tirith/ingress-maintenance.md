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
deleted. Both are committed (`manifests/traefik-canary.yaml`,
`manifests/traefik-canary-b.yaml`) and are deliberately **absent from
`pelargir/manifests.nix`** — they are applied with `kubectl apply -f` and deleted.


They now live in `experiments/traefik-canary/`, outside the manifest tree, so their
exclusion from auto-deploy is structural rather than something a reader must infer.

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
6. `docker start e230f30a9d3f` — never a compose recreate.
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
