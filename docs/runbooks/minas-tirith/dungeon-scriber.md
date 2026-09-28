# Dungeon Scriber Phase 1 on k3s

The binding plan is ADR 0010 in the Dungeon Scriber repository. This runbook covers what
this repository declares and how to operate it. Every step that changes the fleet (a
rebuild, a release gate, a secret, a dataset, anything on nardol) needs the owner's
approval for that exact action.

## Declared shape

`hosts/nixos/minas-tirith/dungeon-scriber-release.nix` holds the release gates and every
host-specific value: the node, the storage class, the blob dataset, the tailnet port, the
tailnet HTTPS switch and the API settings. `pelargir/dungeon-scriber-manifest.nix` renders
`manifests/dungeon-scriber.yaml.in` from it, and pelargir delivers the result as the frozen
AddOn `minas-dungeon-scriber.yaml`. minas reads the same file for its firewall rule, the
optional Serve unit and the backup expectations.

- Namespace `dungeon-scriber`, with Restricted Pod Security enforced.
- PostgreSQL 17 with pgvector 0.8.1, digest-pinned, as a StatefulSet on a
  `local-path-retain` PVC (`postgres-data`, on minas' LUKS root NVMe). A NetworkPolicy
  admits only the API and the migration Job.
- The content-addressed blob store (ADR 0006) on a static `local` PersistentVolume,
  `dungeon-scriber-blobs` (Retain). It points at a hand-created ZFS dataset on the
  `storage` pool, because ADR 0006 needs local POSIX rename and fsync.
- A migration Job, `dungeon-scriber-migrate-<first 12 hex of the API digest>`, running
  `node packages/db/dist/migrate.js` from the API image. It is the only writer of schema.
- An API Deployment: one replica, Recreate, non-root (uid 1000), read-only root
  filesystem, port 3001, `/ready` for startup and readiness, `/health` for liveness. Two
  init containers run before it:
  - `require-blob-dataset` refuses a blob volume without the dataset's sentinel;
  - `require-current-schema` waits on `migrate.js --check` until the schema is current.
- The Pod template carries `dungeon-scriber.saldivar.io/config-sha256`, a hash of the
  complete rendered API ConfigMap data. Editing any key, `TRUST_PROXY_HOPS` above all,
  rolls the Pod even though the settings arrive through `envFrom`.
- An `api-ingress` NetworkPolicy. Host traffic is always admitted: kubelet probes, and
  Serve's loopback proxy. k3s' embedded kube-router controller admits traffic from the
  local node to its Pods regardless of policy, so the probes passing after
  enable is the check that this holds. Tailnet clients (`100.64.0.0/10`) are admitted
  on 3001 only while exposure is on and Serve is off. Every other Pod, except Traefik
  while the public route is on, is refused.
- No Ingress and no LoadBalancer. While `public.enable` is on, minas' Traefik routes
  `dungeon.saldivar.io` to the `api` Service (see "Public origin"), and `api-ingress`
  admits the Traefik Pods. `api-tailnet` is a ClusterIP Service until
  `tailnetExposure` makes it a NodePort (30080). The port is in the low band that
  Kubernetes prefers to leave for explicit assignment, which makes a collision unlikely
  but not impossible, so check it is free before raising exposure (below).

Secrets reach Pods only as mounted files. The API and migration containers read
`DATABASE_URL` and `INTERNAL_WORKER_TOKENS` from those files and export them inside their
own shell. They are never `secretKeyRef` env, whose resolved values persist in
containerd's on-disk metadata.

## What merging changes with every gate off

Merging this into `master` and rebuilding does not start a Pod, create a Secret or read a
SOPS key. It does change the following:

- **pelargir** delivers the AddOn, so the cluster gains:
  - the `dungeon-scriber` Namespace;
  - the `postgres-data` PVC (Pending: its storage class binds on first consumer, and
    there is none);
  - the cluster-scoped `dungeon-scriber-blobs` PV and the `blob-data` PVC bound to it
    (nothing mounts it, and kubelet does not look at the path until a Pod does);
  - the ClusterIP Services `postgres` (headless), `api` and `api-tailnet`, which have no
    endpoints;
  - the ConfigMap;
  - the NetworkPolicies `postgres-ingress` and `api-ingress`;
  - a StatefulSet and a Deployment, both at zero replicas;
  - the migration Job, suspended.
- **minas** gains one raw-table rule per IP family. It drops TCP 30080 addressed to minas'
  own addresses on every interface except `tailscale0`. Nothing listens there, so no
  traffic changes. It is unconditional (see below).
- **minas' backup program** has the Dungeon Scriber blocks, but they are inert:
  - the rsync exclusion matches no directory;
  - the encrypted-dump branch matches no container;
  - blob mirroring is armed only by `/var/lib/healthcheck-ping/dungeon-scriber.expected`,
    which does not exist.

The PVCs and the PV are declared from the start, like PinCollector's. Adding them only
when `staged` would make lowering the gate prune the claims and strand the Retain volume
in `Released`.

## Release gates

The release contract refuses any other combination. Each gate is its own reviewed commit
and its own approved rebuild.

Raising, in this order:

1. `runtimeSecretReady`, once `secrets/dungeon-scriber.yaml` exists with the runtime keys.
2. `registryPullSecretReady`, once that document also holds `ghcr_dockerconfigjson`.
3. `staged`. This starts PostgreSQL only. `gitRevision`, `apiImage` and
   `apiImageRevision` are already set to a verified release.
4. `enabled`: unsuspends the migration Job and raises the API to one replica.
5. `tailnetExposure`: turns `api-tailnet` into a NodePort and admits tailnet clients.
   First confirm that no Service already holds the port. This must print nothing:

   ```sh
   sudo k3s kubectl get svc -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} {.spec.ports[*].nodePort}{"\n"}{end}' | grep -w 30080
   ```

6. Optionally, Serve, in two separate commits: `tailnet.https = true`, then
   `api.trustProxyHops = 1` (below).

Lowering is the exact reverse, one gate per rebuild. The contract refuses a skipped step:
`tailnet.https` needs `tailnetExposure`, `tailnetExposure` needs `enabled`, and `enabled`
needs `staged`.

1. If Serve is on, turn it off in two separate commits, as given under "Tailnet HTTPS"
   below. First `api.trustProxyHops = 0` alone, waiting for the rollout. Only then
   `tailnet.https = false`.
2. `tailnetExposure = false`: rebuild pelargir. `api-tailnet` returns to ClusterIP.
3. `enabled = false`: the API goes to zero and the migration Job is suspended.
   PostgreSQL keeps running.
4. `staged = false`: PostgreSQL goes to zero too.

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

The sentinel lives inside the dataset, so it disappears whenever the dataset is not
mounted. kubelet checks only that the PV's path exists, and an unmounted dataset leaves an
empty mountpoint directory behind, which mounts fine. What refuses it is:

- the API's `require-blob-dataset` init container, which fails without the sentinel, so
  the API never initialises a store on the bare mountpoint;
- the backup, which requires `findmnt --mountpoint` to report exactly
  `storage/dungeon-scriber/blobs` as well as the sentinel.

The `storage` pool has no native encryption, the same as immich's photos. Adding
`-o encryption=on` to this dataset would need key-loading plumbing that minas does not
have, and it is the owner's decision.

### The tailnet gate on minas

`minas-tirith/dungeon-scriber.nix` adds a raw-table rule for the NodePort (30080). While
Serve is off it drops the port on every interface except `tailscale0`; while Serve is on,
on every interface except `lo`. The rule matches only packets addressed to minas itself
(`-m addrtype --dst-type LOCAL`), which is what NodePort traffic is before kube-proxy's
DNAT. Pod traffic that minas forwards elsewhere on the same port number is untouched.
kube-proxy's NodePort DNAT bypasses nixos-fw, and tailscaled accepts all of `tailscale0`
ahead of it, so this rule is the only thing that decides.

The rule is unconditional. Gating it on a release gate would make the NodePort's
protection depend on rebuilding minas before pelargir, which nothing enforces across two
hosts. Rebuild **minas before** raising `tailnetExposure`, then confirm it:

```sh
sudo iptables -t raw -S PREROUTING | grep 30080
sudo ip6tables -t raw -S PREROUTING | grep 30080
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

Rebuild **pelargir first** when raising a gate, since pelargir delivers the manifests
(Serve has its own order, below). The first rebuild that adds both the namespace and its
Secret may exit 4. That self-heals when `k3s-apply-secrets` restarts. Expect one
`ApplyManifestFailed` for the new namespace, because `minas-dungeon-scriber.yaml` sorts
before `minas-namespaces.yaml`.

Arm the backup **before `enabled`**, so the first acceptance backup already has to carry
both halves:

```sh
sudo touch /var/lib/healthcheck-ping/dungeon-scriber.expected   # on minas
```

From then on, a night without the encrypted dump, or without a mounted dataset carrying
its sentinel, reports degraded rather than passing as a backup.

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

The API's init containers wait for the dataset sentinel and then loop on the read-only
`node packages/db/dist/migrate.js --check` (exit 3 while migrations are pending) until the
Job has migrated the schema, so a new API never serves against an older schema. The API
becoming Ready also proves that the `api-ingress` policy admits kubelet's probes.

After the first backup following `enabled`, confirm that the night's snapshot holds both
`k8s-dungeon-scriber-postgres.sql.gz.age` and `k8s-dungeon-scriber-blobs/`, and that
`/var/lib/backup-root-data.degraded` names nothing of Dungeon Scriber's. Use that snapshot
for the restore drill.

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

## Clients and the nardol worker

With `tailnetExposure` on, the API answers plain HTTP at `http://minas-tirith:30080`
(MagicDNS), from tailnet members only. WireGuard encrypts the path, but both current
clients refuse a plain-HTTP origin that isn't loopback:

- the worker accepts only HTTPS, or plain HTTP on loopback;
- the iOS app requires HTTPS, apart from loopback or RFC 1918 addresses in DEBUG builds.

Neither client is loosened. The supported route is HTTPS on the tailnet through
`tailscale serve`, which keeps ADR 0010 §5 intact.

### Tailnet HTTPS (tailscale serve)

`tailnet.https` in the release file turns this on for both hosts:

- `minas.dungeonScriber.tailnetServe.enable` follows it by default, and an assertion
  refuses the two disagreeing;
- pelargir renders an `api-ingress` that admits no direct tailnet client. The API
  trusts Serve's hop only once `api.trustProxyHops = 1` is set, in a separate commit.

On minas it adds `dungeon-scriber-tailnet-serve.service`, which runs:

```sh
tailscale serve --bg --https=443 http://127.0.0.1:30080
```

tailscaled then terminates TLS with a tailnet certificate for minas' MagicDNS name. While
Serve is on, minas' raw-table rule closes the NodePort on every interface except
loopback, and `api-ingress` admits only host traffic. HTTPS is then the only way in.
Disabling Serve stops the unit, whose stop step runs `tailscale serve --https=443 off`.

**Never change `tailnet.https` and `api.trustProxyHops` in the same commit.** The API may
trust Serve's hop only while direct clients are shut out, both at the NodePort (minas) and
at `api-ingress` (pelargir). Kubernetes does not order a NetworkPolicy change against a
rollout, so each transition goes through the intermediate state `tailnet.https = true`
with `api.trustProxyHops = 0`. In that state Serve fronts the API, direct clients are
refused, and the API trusts nothing. The contract allows that state, rejects one hop
without Serve, and rejects more than one hop with it. That keeps each commit safe on its
own, but the two-commit order is the operator's to keep.

Turning Serve on:

1. In the Tailscale admin console, under DNS, confirm MagicDNS is on and enable
   **HTTPS Certificates**. This tailnet has none today. Certificates are issued through
   public Certificate Transparency logs, so minas' `*.ts.net` name becomes public
   knowledge.
2. `tailnetExposure` is already on. On minas, confirm that loopback reaches the NodePort,
   because Serve depends on kube-proxy's localhost NodePorts:

   ```sh
   curl -fsS http://127.0.0.1:30080/health
   ```

3. Commit `tailnet.https = true`, keeping `api.trustProxyHops = 0`.
4. Rebuild **minas**. Serve starts, and the NodePort closes to everything but loopback.
   Then rebuild **pelargir**. `api-ingress` stops admitting tailnet clients, and the API
   does not roll because its settings are unchanged.
5. Confirm that the policy is closed:

   ```sh
   sudo k3s kubectl -n dungeon-scriber get networkpolicy api-ingress -o jsonpath='{.spec.ingress}'
   ```

   This prints `[]` or nothing. A tailnet client routed to the Pod IP on 3001 must now
   time out.
6. Commit `api.trustProxyHops = 1`, and rebuild **pelargir**. The settings hash changes,
   so the API rolls to trust exactly Serve's hop.
7. Check with `sudo tailscale serve status` on minas, and from a tailnet client with
   `curl -fsS https://minas-tirith.<tailnet>.ts.net/health`.

Turning Serve off (the reverse, one commit at a time):

1. Commit `api.trustProxyHops = 0`, keeping `tailnet.https = true`, and rebuild
   **pelargir**. The policy stays closed while the API rolls to trust no hop.
2. Wait for the rollout to finish, and for the old Pod to be gone, before going on:

   ```sh
   sudo k3s kubectl -n dungeon-scriber rollout status deploy/api
   sudo k3s kubectl -n dungeon-scriber get pods -l app=dungeon-scriber-api \
     -o jsonpath='{range .items[*]}{.metadata.name} {.metadata.annotations.dungeon-scriber\.saldivar\.io/config-sha256}{"\n"}{end}'
   ```

   Exactly one Pod, carrying the new hash, and `TRUST_PROXY_HOPS` is `0` in the ConfigMap.
3. Commit `tailnet.https = false`. Rebuild **pelargir** (`api-ingress` admits tailnet
   clients again, with no rollout), then **minas** (Serve stops, and the NodePort reopens
   to `tailscale0`).

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

Once `dungeon-scriber.expected` exists, any of these marks the backup degraded:

- a missing dump;
- the dataset not mounted at the blob root;
- a missing sentinel;
- a failed encryption or prune.

Nothing is pruned from the mirror unless the dataset is mounted and the sentinel is
present.

## Restore drill

Required before launch (the revival specification's launch gate). Run it against scratch
targets, never the live namespace, and use the first snapshot taken after the backup was
armed and the API enabled:

1. Choose that snapshot, for example `storage2/backup@daily-<ts>`. From
   `/storage2/backup/.zfs/snapshot/<name>/dumps/`, read **both** halves of the pair, the
   dump and the blob mirror. A snapshot missing either half fails the drill.
2. On a machine holding an admin or pelargir age identity, decrypt the dump
   (`age -d -i <identity> k8s-dungeon-scriber-postgres.sql.gz.age | gunzip`). Restore it
   into a disposable PostgreSQL 17 with pgvector 0.8.1.
3. Decrypt every mirror entry into a scratch blob root at the same `<aa>/<bb>/<sha256>`
   path, and verify that each file's SHA-256 equals its name. Write the
   `.dungeon-scriber-blob-root` sentinel there.
4. Point a disposable API at that database and blob root. Confirm that every blob the
   database references exists in the scratch root, and that sessions play back.
5. Record the snapshot name and the counts: tables, rows, blobs referenced, and blobs
   verified.

A real restore replaces the live data, which is a separately approved data mutation. Lower
the gates in the reverse order above, down to `enabled = false`: Serve off in its two
commits first, then exposure, then `enabled`. Keep `staged` on so PostgreSQL runs to
receive the restore. Then:

1. Restore the database into the retained PVC's cluster, and decrypt the blobs into the
   mounted dataset, keeping its sentinel.
2. Raise the gates again in the forward order: `enabled`, then `tailnetExposure`, then
   Serve in its two commits.

## Rollback

Rollback lowers gates in the reverse order given under "Release gates", one gate and one
rebuild at a time. When Serve is on, it goes first, in its two commits (hops, then
`tailnet.https`). The contract refuses any skipped step, so, for example, `enabled = false`
is accepted only after `tailnetExposure = false`.

- `enabled = false` leaves PostgreSQL running.
- `staged = false` scales everything to zero with the Job suspended.

The PVCs and the blob PersistentVolume are Retain and survive. To return to an earlier
image, restore the previous release values; database migrations do not roll back. Never
remove the manifest from the catalog or rename it. Each of these is a deployment action
that needs its own authority.

## Public origin (`dungeon.saldivar.io`)

ADR 0010 §4. `public.enable` in the release file turns on both halves:

- **minas:** the `k8s-dungeon-scriber` router in the Traefik file provider
  (`traefik-routes/catalog.nix`), on the `https` entrypoint with the existing
  `*.saldivar.io` certificate. Its backend is
  `http://api.dungeon-scriber.svc.cluster.local:3001`.
- **pelargir:** a second `api-ingress` rule. It has one peer that combines a namespace
  selector (`traefik`) and a Pod selector (`app: traefik`), so it admits the Traefik Pods
  and nothing else in that namespace, on 3001 only. Traefik runs with a Pod IP (hostPort,
  not hostNetwork), so it is not host traffic.

What reaches the API is exactly: Traefik (public), host traffic on minas (Serve's
loopback proxy and kubelet probes, which k3s' network-policy controller always admits
from the local node), and nothing else. minas runs no hostNetwork Pods. Host network
Pods on other nodes (Home Assistant and similar on pelargir and osgiliath) arrive from
another node's address, so they are refused like any other Pod. That is what makes
`TRUST_PROXY_HOPS=1` safe: every peer that can reach port 3001 is one of the two
single-hop proxies.

The route is an **allowlist**, with explicit denials on top that hold even if the
allowlist is ever widened:

- `Host(dungeon.saldivar.io) && (PathPrefix(/v1/) || Path(/health))` is routed.
- Any decoded path containing `..`, `//` or `%` is refused.
- `!Path(/ready)` and `!PathPrefix(/internal)` are part of the rule as well.
- Everything else on the host matches no router and gets Traefik's 404.

That includes `/`, `/ready` (dependency I/O for kubelet, which probes the Pod IP directly
and so is unaffected) and `/internal/v1` (the worker protocol; workers use the tailnet
Serve origin only). The dot-segment rule is not decorative. Traefik forwards dot
segments unresolved. nixpkgs' Traefik 3.7.8 (the flake's pin, used by the edge check)
routed `/v1/..%2finternal/v1/...` to the backend under `/v1/` until the rule was added.

The route has one middleware, `k8s-dungeon-scriber-headers`, defined in the same file:

- `Strict-Transport-Security: max-age=31536000`, this host only, no preload;
- `X-Content-Type-Options: nosniff`.

It carries no Authentik gate (the app has its own login) and no buffering middleware,
so request bodies (chunked audio) stream to the API. The API enforces its own
per-route body limits. Time is bounded at two layers:

- **The https entrypoint's defaults:** 60 s to read a whole request including its body,
  and 180 s keep-alive idle. These are static args in `manifests/traefik.yaml`, left
  untouched, since any `spec.template` change there recreates the singleton ingress.
- **This route's own `k8s-dungeon-scriber` serversTransport:**
  - 5 s to connect to the API;
  - 60 s from the end of the request to the response headers (SSE live streams send
    theirs at once, then heartbeats);
  - 90 s idle for pooled backend connections.

Traefik trusts forwarded headers only from Cloudflare's ranges, and this name is DNS
only. The entrypoint's single forwarded-header setting in `manifests/traefik.yaml` is
`--entrypoints.https.forwardedHeaders.trustedIPs=@cloudflareTrustedIPsV4@`, which
pelargir renders from `pelargir/cloudflare-ranges.nix`. There is no `insecure`, and the
plain `http` entrypoint only redirects. For every other peer, Traefik discards the incoming `X-Forwarded-*` headers
and sets the peer's own address, so the rightmost `X-Forwarded-For` entry is always the
real client. The API's one trusted hop reads exactly that entry. Serve appends the
tailnet peer address the same way. Other headers, `X-Request-Id` included, pass
unchanged. Whether the API adopts an incoming request ID is the application's choice.

These are proved offline by `checks/dungeon-scriber-edge-contract.nix`, which serves the
production-rendered route with nixpkgs' Traefik and checks all of the following:

- the allowlist and the escape attempts (dot segments, encoded slashes, `/internal`,
  `/ready`);
- HSTS and nosniff on routed responses;
- that a forged `X-Forwarded-For` is replaced;
- that `X-Request-Id` passes through, and that a 5 MB body arrives intact.

`checks/dungeon-scriber-deployment-contract.nix` pins the forwarded-header trust in
`traefik.yaml`, the policy's peers, and the route's middlewares and transport.

The release contract accepts one hop behind Traefik, Serve or both. It still refuses a
hop while direct tailnet clients are admitted (exposure with Serve off), whether or not
the public route is on, because such a client could forge the header. The tailnet
Serve path is unchanged, and workers keep using `https://minas-tirith.<tailnet>.ts.net`.

`dungeon.saldivar.io` is also in `traefik-hostnames.nix`. So minas and the cluster's
CoreDNS resolve it to minas' LAN address, like every other minas-terminated name.

### Cutover order

Merging changes these things, and nothing else:

- a new router file on minas;
- one `api-ingress` rule on pelargir;
- a CoreDNS `coredns-custom` entry and a minas `/etc/hosts` entry.

The rendered `minas-traefik.yaml` is byte-identical, so Traefik does not restart. The
API does not roll, because its settings and Pod template are unchanged.

1. Merge the route commit. Rebuild **pelargir first**, which delivers the policy, then
   **minas**, which delivers the route file. Traefik reloads its file provider live.
   Confirm:

   ```sh
   sudo k3s kubectl -n dungeon-scriber get networkpolicy api-ingress -o jsonpath='{.spec.ingress}'
   ls -l /usr/local/etc/traefik/k8s-dungeon-scriber.yml          # on minas
   curl -fsS --resolve dungeon.saldivar.io:443:10.0.1.6 https://dungeon.saldivar.io/health
   ```

   The last command runs from a LAN host. It must return 200 with a valid certificate
   before DNS changes.
2. The owner changes the Cloudflare record: `dungeon.saldivar.io` becomes a CNAME to
   minas' public name, DNS only (grey cloud), like the other minas hostnames. This
   repository does not hold DNS.
3. From outside both houses:

   ```sh
   dig +short dungeon.saldivar.io                                # minas' public address
   curl -sS -o /dev/null -w '%{http_code}\n' https://dungeon.saldivar.io/         # 404 (no router)
   curl -fsSI https://dungeon.saldivar.io/health     # 200, Strict-Transport-Security and nosniff present
   for p in /ready /internal/v1/ /v1/..%2finternal/v1/ /v1/%2e%2e/internal/v1/; do
     curl -sS --path-as-is -o /dev/null -w "%{http_code} $p\n" "https://dungeon.saldivar.io$p"
   done                                              # every one 404
   ```

   Then upload a real recording from the phone against the public origin. In the API
   logs (`kubectl -n dungeon-scriber logs deploy/api`), check that the request's client
   address is the phone's public address, not Traefik's Pod IP.
4. Merge the baseline commit and rebuild **pelargir**. The external ingress monitor
   (`minas-ingress-external`) now expects `404` for `GET /`. It pages after three
   consecutive failures, about 15 minutes. If the baseline lands before DNS and the route
   are live, it pages, so keep this step last. If it lands in the same merge, finish steps
   1 to 3 within that window.

### Rollback

1. Set `public.enable = false`. Rebuild **minas first**, so the router file becomes
   `http: {}` and the name stops routing. Then rebuild **pelargir**, which removes the
   Traefik rule from `api-ingress`. That is the reverse of the cutover: close the door
   before withdrawing the policy.
2. Restore the `dungeon.saldivar.io 000ERR` baseline line, and ask the owner to point
   the record back at `pelargir.saldivar.io`, if the name should stop resolving to minas.
   The ROADMAP constraint to keep the record stands.
3. `api.trustProxyHops` stays 1 for as long as Serve is on. If Serve is also off, lower it
   first, as the Serve procedure describes.

The route's filename (`k8s-dungeon-scriber.yml`) stays managed while disabled, so rollback
never leaves a stale router being served.
