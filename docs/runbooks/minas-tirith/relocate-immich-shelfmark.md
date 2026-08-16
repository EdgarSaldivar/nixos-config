# Relocating immich and shelfmark off `/home/edgar/git/docker`

**Status: PREPARED, NOT EXECUTED.** This is a supervised procedure. Nothing in it
has been applied, and the manifests in this repository still point at the old
paths until you complete step 4.

## Why

`/home/edgar/git/docker` is the pre-migration Docker Compose checkout. Its
containers are gone; its *directory* is still the on-disk config store for three
live workloads plus the ingress route drop point.
`checks/external-checkout-dependency.nix` pins the exact set — **five bindings**
today. This procedure removes two of them, leaving only the traefik pair, which is
the genuinely hard one because it touches ingress for 26 hostnames.

immich and shelfmark are the easy two: independent of each other, independent of
traefik, and each is one config directory.

Target paths follow the convention every other migrated workload already uses —
nine of them live under `/usr/local/etc/<service>`; these two are the outliers:

| workload | from | to |
|---|---|---|
| immich | `/home/edgar/git/docker/immich/config` | `/usr/local/etc/immich/config` |
| shelfmark | `/home/edgar/git/docker/books/shelfmark/config` | `/usr/local/etc/shelfmark/config` |

⚠️ **shelfmark carries `users.db`**, which is a *declared* dump target in
`backup-root-data.nix`. Its declaration moves in the same commit or the dump
silently stops — and "silently" is the operative word: the never-created check
only knows the names it is given.

---

## Order, and why it is this order

Do **immich first, completely, and verify it**, before touching shelfmark. They are
independent, so a problem with the first tells you something about the method
before you have two workloads in flight. Do not batch them.

Within each workload the order is: **copy → verify copy → switch manifest →
rebuild pelargir → verify Pod → only then remove the old tree.** The old directory
is the rollback, so it is the last thing to go, and it goes in a *separate*
session after the workload has run for a while.

## 1. Copy the config tree (on minas, workload still running)

`rsync` rather than `mv`: the source stays intact as the rollback.

```sh
sudo mkdir -p /usr/local/etc/immich
sudo rsync -aHAX --numeric-ids \
  /home/edgar/git/docker/immich/config/ \
  /usr/local/etc/immich/config/
```

`-a` preserves ownership and mode, which matters: immich's containers run as uid 0
and the manifest deliberately sets no `runAsUser`/`fsGroup`, so the copy must not
normalise ownership.

## 2. Verify the copy before trusting it

```sh
sudo diff -r /home/edgar/git/docker/immich/config /usr/local/etc/immich/config && echo IDENTICAL
sudo find /usr/local/etc/immich/config -newer /usr/local/etc/immich | head
```

⛔ A byte-identical tree is necessary and not sufficient — the app is **running**
and may write during the copy. Either quiesce it first (`kubectl scale
deploy/immich --replicas=0`, then copy, then proceed) or re-run the `rsync` and
`diff` immediately before step 3 so the window is seconds rather than minutes.
For immich, quiescing is cheap; prefer it.

## 3. Switch the manifest

In `hosts/nixos/minas-tirith/manifests/immich.yaml`, the `config` volume:

```yaml
        - name: config
          hostPath: { path: /usr/local/etc/immich/config, type: Directory }
```

Delete the now-stale comment above it (`✅ The config tree is on cr_root and is a
backup source.` — still true, but it sat there to explain the old location).

Then delete the corresponding entry from `expected` in
`checks/external-checkout-dependency.nix`. **The build fails until you do** — that
is the ratchet working, not a problem to route around.

## 4. Deploy

⛔ **pelargir first.** The manifest is delivered by pelargir's auto-deploy
directory, not by minas. Rebuilding minas alone changes nothing and has caused the
only outage of the migration.

```sh
# from the repo, per AGENTS.md: rsync, commit, rsync again, then switch
nix flake check                       # the ratchet must be green again
bash scripts/closure-equiv.sh .       # expect: pelargir moves, others do not
```

Then rebuild pelargir, and watch the AddOn actually apply:

```sh
kubectl -n kube-system get addon minas-immich.yaml -o yaml | tail -20
kubectl -n immich rollout status deploy/immich --timeout=180s
```

## 5. Verify the workload, not just the Pod

A Running Pod proves the mount resolved, nothing more.

```sh
kubectl -n immich get pod -o wide
kubectl -n immich exec deploy/immich -- ls -la /config | head
# and from outside: the actual application
curl -sS -o /dev/null -w '%{http_code}\n' https://photos.saldivar.io
```

## 6. Repeat for shelfmark — plus the backup declaration

Same six steps, with one addition. In `backup-root-data.nix` the declared SQLite
dump list contains:

```
"/home/edgar/git/docker/books/shelfmark/config/users.db|" \
```

Change it to `/usr/local/etc/shelfmark/config/users.db` **in the same commit** as
the manifest. Then confirm the dump still happens:

```sh
sudo systemctl start backup-root-data
ls -la /storage2/backup-dumps/ | grep -i shelf
```

⏳ If that file is missing or stale after a run, stop and fix it before removing
the old tree. A backup that silently stopped is the failure mode this whole
session has been chasing.

## 7. Only afterwards: remove the old trees

Not in the same session. Let both workloads run normally for at least one full
backup cycle, then:

```sh
sudo rm -rf /home/edgar/git/docker/immich/config
sudo rm -rf /home/edgar/git/docker/books/shelfmark/config
```

## Rollback

At any point before step 7 the old tree is intact. Revert the manifest commit,
rebuild **pelargir**, and the Pod returns to the old path. Because the change is a
`hostPath` value and not a Service or a name, there is no AddOn ownership
transfer and no ClusterIP churn — this is one of the few cluster changes in this
repository that is genuinely a plain revert.

## When the ratchet reaches zero

Three bindings remain after this: the traefik file-provider pair (which must move
together) and nothing else. When
`checks/external-checkout-dependency.nix` has an empty `expected`, delete that
check and `/home/edgar/git/docker` in the same commit.
