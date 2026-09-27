# Dungeon Scriber Phase 1 on k3s

The binding plan is ADR 0010 in the Dungeon Scriber repository. This runbook covers what
this repository declares and how to operate it. Every step that changes the fleet (a
rebuild, a release gate, a secret, a dataset, anything on nardol) needs the owner's
approval for that exact action.

## Declared shape

`hosts/nixos/minas-tirith/dungeon-scriber-release.nix` holds the release gates and every
host-specific value: the node, the storage class, the blob dataset path, the tailnet port
and the API settings. `pelargir/dungeon-scriber-manifest.nix` renders
`manifests/dungeon-scriber.yaml.in` from it, and pelargir delivers the result as the frozen
AddOn `minas-dungeon-scriber.yaml`.

- Namespace `dungeon-scriber`, with Restricted Pod Security enforced.
- PostgreSQL 17 with pgvector 0.8.1, digest-pinned, as a StatefulSet on a
  `local-path-retain` PVC (`postgres-data`, on minas' LUKS root NVMe). A NetworkPolicy
  admits only the API and the migration Job.
- The content-addressed blob store (ADR 0006) on a static `local` PersistentVolume,
  `dungeon-scriber-blobs` (Retain). It points at a hand-created ZFS dataset on the
  `storage` pool, because ADR 0006 needs local POSIX rename and fsync.
- A migration Job, `dungeon-scriber-migrate-<first 12 hex of the API digest>`, running
  `node packages/db/dist/migrate.js` from the API image.
- An API Deployment: one replica, Recreate, non-root (uid 1000), read-only root
  filesystem, port 3001, `/ready` for startup and readiness, `/health` for liveness.
- No Ingress, no Traefik route, no LoadBalancer. `api-tailnet` is a ClusterIP Service
  until `tailnetExposure` makes it a NodePort.

Secrets reach Pods only as mounted files. The API and migration containers read
`DATABASE_URL` and `INTERNAL_WORKER_TOKENS` from those files and export them inside their
own shell. They are never `secretKeyRef` env, whose resolved values persist in
containerd's on-disk metadata.

## Release gates, in order

The release contract refuses any other order, and while `staged = false` everything renders
inert (zero replicas, the Job suspended). Each gate is its own reviewed commit and its own
approved rebuild.

1. `runtimeSecretReady`, once `secrets/dungeon-scriber.yaml` exists with the runtime keys.
2. `registryPullSecretReady`, once that document also holds `ghcr_dockerconfigjson`.
3. `staged`. This starts PostgreSQL only. `gitRevision`, `apiImage` and
   `apiImageRevision` are already set to a verified release.
4. `enabled`: unsuspends the migration Job and raises the API to one replica.
5. `tailnetExposure`: turns `api-tailnet` into a NodePort.

For each new release, take the digest from the Dungeon Scriber API image publish job. Before
setting `apiImageRevision`, inspect the pushed image's `org.opencontainers.image.revision`
label independently; both revisions must equal the reviewed 40-character commit. The
contract rejects nulls, tags, the all-zero placeholder, other repositories and revision
mismatches.

## Before the first deploy

### Blob dataset on minas

disko may never touch the pools, so the owner creates the dataset by hand:

```sh
sudo zfs create -p -o mountpoint=/storage/dungeon-scriber/blobs storage/dungeon-scriber/blobs
sudo chown 1000:1000 /storage/dungeon-scriber/blobs
sudo chmod 0700 /storage/dungeon-scriber/blobs
sudo touch /storage/dungeon-scriber/blobs/.dungeon-scriber-blob-root
```

The sentinel file is what the backup reads as proof that the dataset is mounted. Without
it, an unmounted dataset would look like an empty store. kubelet refuses to mount a
missing path, so the API stays Pending, rather than initialising a store, if the dataset
is absent. The `storage` pool has no native encryption, the same as immich's photos.
Adding `-o encryption=on` to this dataset would need key-loading plumbing that minas does
not have, and it is the owner's decision.

### The tailnet gate on minas

`minas-tirith/dungeon-scriber.nix` adds a raw-table rule that drops the NodePort (30301) on
every interface except `tailscale0`. kube-proxy's NodePort DNAT bypasses nixos-fw, and
tailscaled accepts all of `tailscale0` ahead of it, so this rule is the only thing that
decides. The rule is unconditional. Rebuild **minas before** raising `tailnetExposure`,
then confirm it:

```sh
sudo iptables -t raw -S PREROUTING | grep 30301
sudo ip6tables -t raw -S PREROUTING | grep 30301
```

Other nodes also open the NodePort. `externalTrafficPolicy: Local` means only the node
running the API has an endpoint, so every other node drops the connection.

### SOPS document

Create `secrets/dungeon-scriber.yaml` with `sops secrets/dungeon-scriber.yaml`. `.sops.yaml`
encrypts it to the admin and pelargir recipients only. The keys:

| Key | Value |
| --- | --- |
| `postgres_password` | A new random password, hex so it needs no URL escaping |
| `database_url` | `postgres://dungeon_scriber:<that password>@postgres:5432/dungeon_scriber` |
| `internal_worker_tokens` | `<workerId>:<sha256 hex of the worker token>`, comma-separated for several workers |
| `ghcr_dockerconfigjson` | Compact Docker config JSON for a read-only GHCR token scoped to `dungeon-scriber-api` |

Mint a worker token on the Mac and keep only its hash in this document. The raw token goes
to the worker alone and never enters this repository:

```sh
token=$(openssl rand -hex 32)
printf %s "$token" | shasum -a 256   # the hash half of internal_worker_tokens
```

The GHCR package is private. The pull credential uses PinCollector's mechanism exactly:
compact Docker config JSON under `ghcr_dockerconfigjson` in this app's own SOPS document,
applied by `k3s-apply-secrets` as a `kubernetes.io/dockerconfigjson` Secret that every Pod
names in `imagePullSecrets`. The same read-only token can serve both apps if its package
access covers `edgarsaldivar/dungeon-scriber-api`. Otherwise mint one that does.
`scripts/provision-ghcr-credential.py` is hard-wired to PinCollector's document and
packages. Store the registry credential with the same hidden-input discipline: never pass
it in argv, and never write it to a plaintext file. `checks/dungeon-scriber-secret-contract.nix`
fails the build if the document exists with any plaintext value, or if the declared keys
and the applier disagree.

`k3s-apply-secrets` on pelargir builds `dungeon-scriber-runtime` (`postgres-password`,
`database-url`, `internal-worker-tokens`) and `dungeon-scriber-registry` with
`kubectl create secret --from-file`, piped straight into the API.

## First deploy

Rebuild **pelargir first** for every gate, since pelargir delivers the manifests. The
first rebuild that adds both the namespace and its Secret may exit 4. That self-heals when
`k3s-apply-secrets` restarts. Expect one `ApplyManifestFailed` for the new namespace,
because `minas-dungeon-scriber.yaml` sorts before `minas-namespaces.yaml`.

After `enabled`:

```sh
sudo k3s kubectl -n dungeon-scriber get pods,pvc,pv,jobs
sudo k3s kubectl -n dungeon-scriber logs job/dungeon-scriber-migrate-<digest prefix>
sudo k3s kubectl -n dungeon-scriber get deploy api -o jsonpath='{.metadata.annotations}'
```

Acceptance requires all of the following:

- the Job has completed;
- `postgres-0` and the API are Ready;
- the API image ID is the release digest, and its annotation matches `gitRevision`;
- `/ready` returns 200 from inside the cluster.

The API's `require-current-schema` init container loops on the read-only
`node packages/db/dist/migrate.js --check` (exit 3 while migrations are pending) until the
Job has migrated the schema, so a new API never serves against an older schema. The Job is
the only writer of schema.

### First owner account

`bootstrap-owner.js` creates the first account only if no user exists. Run it inside the API
Pod, feeding the password on stdin so it never appears in argv, the environment of a
manifest, or a file on disk (`/tmp` is a memory-backed volume):

```sh
read -rs pw
printf '%s' "$pw" | sudo k3s kubectl -n dungeon-scriber exec -i deploy/api -c api -- sh -ec '
  umask 077; f=/tmp/owner-password; trap "rm -f $f" EXIT; cat > "$f"
  DATABASE_URL="$(cat /run/secrets/dungeon-scriber/database-url)" \
  BOOTSTRAP_USERNAME=<owner username> BOOTSTRAP_PASSWORD_FILE="$f" \
    node apps/api/dist/bootstrap-owner.js'
unset pw
```

After the first successful backup cycle, arm the backup expectations:
`sudo touch /var/lib/healthcheck-ping/dungeon-scriber.expected` on minas.

## Clients and the nardol worker

With `tailnetExposure` on, the API answers plain HTTP at `http://minas-tirith:30301`
(MagicDNS), from tailnet members only. WireGuard encrypts the path, but both current
clients refuse a plain-HTTP origin that isn't loopback:

- the worker accepts only HTTPS, or plain HTTP on loopback;
- the iOS app requires HTTPS, apart from loopback or RFC 1918 addresses in DEBUG builds.

Neither client is loosened. The supported route is HTTPS on the tailnet through
`tailscale serve`, which keeps ADR 0010 §5 intact.

### Tailnet HTTPS (tailscale serve)

`minas.dungeonScriber.tailnetServe.enable` (in `hosts/nixos/minas-tirith/dungeon-scriber.nix`,
off by default) adds `dungeon-scriber-tailnet-serve.service`. The unit runs:

```sh
tailscale serve --bg --https=443 http://127.0.0.1:30301
```

tailscaled then terminates TLS with a tailnet certificate for minas' MagicDNS name. While
the option is on, minas' raw-table rule closes the NodePort on every interface except
loopback, so HTTPS is the only way in and no client can bypass the proxy to forge
`X-Forwarded-For`. Disabling the option stops the unit, whose stop step runs
`tailscale serve --https=443 off`.

Before enabling it, the owner does the following:

1. In the Tailscale admin console, under DNS, confirm MagicDNS is on and enable
   **HTTPS Certificates**. This tailnet has none today. Certificates are issued through
   public Certificate Transparency logs, so minas' `*.ts.net` name becomes public
   knowledge.
2. Raise `tailnetExposure`, and set `api.trustProxyHops = 1` in
   `dungeon-scriber-release.nix`. Serve is exactly one proxy hop. The module's
   assertions refuse the option without both.
3. On minas, confirm that loopback reaches the NodePort. Serve depends on kube-proxy's
   localhost NodePorts:

   ```sh
   curl -fsS http://127.0.0.1:30301/health
   ```

4. Set `minas.dungeonScriber.tailnetServe.enable = true`, then rebuild pelargir (for the
   release change) and minas (for the unit and firewall). Check with
   `sudo tailscale serve status`, and from a tailnet client with
   `curl -fsS https://minas-tirith.<tailnet>.ts.net/health`.

Clients then use `https://minas-tirith.<tailnet>.ts.net`, where `<tailnet>` is the
tailnet's DNS name from the admin console. It is deliberately not recorded here.

Port 443 on minas is also Traefik's hostPort. Serve answers tailnet connections to minas'
Tailscale address before the kernel sees them, so tailnet clients can no longer reach
Traefik on 443. Nothing in the fleet does that today (the ingress probe uses public DNS).
If that changes, set `httpsPort = 8443` and use
`https://minas-tirith.<tailnet>.ts.net:8443`.

### The GPU worker

The GPU worker leases jobs from the API over the tailnet with its bearer token. The API
stores only that token's SHA-256 in `internal_worker_tokens`. On nardol, with the owner's
approval (gaming always wins, and inference is leased):

- run the published worker image digest under its existing container runtime;
- give it the raw token as a mode-0600 file through `DS_WORKER_TOKEN_FILE`;
- set `DS_API_BASE_URL` to `https://minas-tirith.<tailnet>.ts.net`.

Rotating a worker token means replacing its hash in the SOPS document, which re-runs the
applier. The API reads `INTERNAL_WORKER_TOKENS` at start, so then restart the API
(`kubectl rollout restart`; separately authorized).

## Backups

`backup-root-data` on minas covers Dungeon Scriber only through the age-encrypted path that
Authentik uses, to the same admin and pelargir recipients:

- PVC directories matching `pvc-*_dungeon-scriber_*` are excluded from the plaintext
  rsync.
- Every PostgreSQL in the namespace is dumped with `pg_dumpall | gzip | age` to
  `/storage2/backup/dumps/k8s-dungeon-scriber-postgres.sql.gz.age`.
- Blobs are mirrored as one age file per blob under
  `/storage2/backup/dumps/k8s-dungeon-scriber-blobs/<aa>/<bb>/<sha256>.age`. Unpublished
  `.incoming` and `.staged` uploads are skipped. Blobs are immutable, so only new audio is
  encrypted each night and unchanged files cost nothing in the snapshots.

The run keeps the database and the blobs at one consistent point. Order matters:

1. Mirror entries for vanished blobs are pruned **before** the dump.
2. The database is dumped.
3. New blobs are mirrored **after** the dump.

So every blob a night's dump references is in that night's snapshot, provided the
application's GC grace period is longer than the backup unit runs (6 hours at most). The
nightly `storage2/backup` snapshots (14 daily, 8 weekly) hold each pair. Purged content
therefore leaves the backups only when those snapshots age out.

Once `dungeon-scriber.expected` exists, any of these marks the backup degraded: a missing
dump, a missing sentinel, or a failed encryption or prune.

## Restore drill

Required before launch (the revival specification's launch gate). Run it against scratch
targets, never the live namespace:

1. Choose one snapshot, for example `storage2/backup@daily-<ts>`, and read both artifacts
   from `/storage2/backup/.zfs/snapshot/<name>/dumps/`.
2. On a machine holding an admin or pelargir age identity, decrypt the dump
   (`age -d -i <identity> k8s-dungeon-scriber-postgres.sql.gz.age | gunzip`). Restore it
   into a disposable PostgreSQL 17 with pgvector 0.8.1.
3. Decrypt every mirror entry into a scratch blob root at the same `<aa>/<bb>/<sha256>`
   path, and verify that each file's SHA-256 equals its name.
4. Point a disposable API at that database and blob root. Confirm that every blob the
   database references exists, and that sessions play back.
5. Record the snapshot name and the counts: tables, rows and blobs verified.

A real restore replaces the live data, which is a separately approved data mutation:

1. Scale the release to `enabled = false`.
2. Restore the database into the retained PVC's cluster.
3. Decrypt the blobs into the dataset.
4. Re-enable.

## Rollback

Set `enabled = false`, which leaves PostgreSQL running, or `staged = false`, which scales
everything to zero with the Job suspended. The PVCs and the blob PersistentVolume are
Retain and survive. To return to an earlier image, restore the previous release values;
database migrations do not roll back. Never remove the manifest from the catalog or rename
it. Each of these is a deployment action that needs its own authority.

## Public cutover (later, separately approved)

ADR 0010 §4 names exactly three changes:

1. Repoint the `dungeon.saldivar.io` DNS record from `pelargir.saldivar.io` to minas'
   public name.
2. Add the `dungeon.saldivar.io` route in `hosts/nixos/minas-tirith/traefik-routes.nix`
   (the route catalog), with the backend `dungeon-scriber/api:3001`. Traefik must
   stream request bodies, preserve request IDs and client IPs, and use upload-friendly
   timeouts.
3. Update the ingress acceptance baseline for `dungeon.saldivar.io` from its intentional
   `000ERR`.

With the route in place, `api.trustProxyHops` stays at 1 (Traefik is also a single hop).
Then decide whether tailnet serve stays on for workers.
