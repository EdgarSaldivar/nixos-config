# Authentik rollout and recovery runbook

This design adds one identity service at `https://auth.saldivar.io` without changing any
existing application login during installation. The initial foundation commit was
deliberately inert: PostgreSQL, worker, and server declared `replicas: 0`, and both
Traefik files were managed empty files. The checked-in accepted Phase A state now declares
all three replicas at `1` and publishes Authentik itself. The accepted Phase B state gates
the nine declared administrator applications plus the Traefik dashboard through Authentik.

In the active steady state, a protected admin URL redirects to Authentik, you use a
passkey, and the same browser session opens the other protected admin tools without
another Authentik prompt. The URLs remain externally reachable through Traefik;
Tailscale is not required. Traefik BasicAuth is not attached to the active routes; its
definition remains available for rollback. Native application authentication and API keys
remain enabled behind Authentik as defense-in-depth. No phone ARR client or direct port
bypass is part of this design.

Phase A installed and proved Authentik only. Phase B was separately authorized and used a
browser-accepted Maintainerr canary before the ARR, download-client, and dashboard waves.
Never use `kubectl scale`: k3s auto-deploy will eventually restore the committed replica
count and turn that drift into an outage.

## Authoritative files

- `manifests/authentik.yaml`: pinned images, retained PVCs, controllers, blueprint, and
  default-deny NetworkPolicies.
- `../pelargir/manifests.nix`: immutable AddOn delivery as
  `minas-workload-authentik.yaml` (the basename is now frozen).
- `../pelargir/secrets.nix`: tmpfs rendering and application of `authentik-secrets`.
- `../../../secrets/authentik.yaml`: encrypted secret key, PostgreSQL password, and
  one-time `akadmin` bootstrap password hash.
- `traefik-routes.nix`: explicit `publish`, `protectedRoutes`, and dashboard switches.
- `monitoring.nix` and `system.nix`: post-promotion latching and encrypted backups.

## Secret creation and rotation

The initial password is stored only as a salted Django hash. Authentik's documented
`hash_password` command waits for a Compose PostgreSQL service even though hashing does
not require a database. This equivalent offline command was tested against the exact
pinned image. It reads one non-empty line from stdin; the plaintext is not a Docker
argument, environment variable, shell-history value, or file:

```sh
pbpaste -Prefer txt | docker run --rm -i --entrypoint python \
  'ghcr.io/goauthentik/server:2026.5.6@sha256:ed120caf710ccf82ef0026f0bc74e51615bc95ebff228a7a2d6fc60c441c3868' \
  -c 'import sys; from django.contrib.auth.hashers import PBKDF2PasswordHasher; raw=sys.stdin.buffer.read().rstrip(b"\r\n"); assert raw and b"\n" not in raw and b"\r" not in raw, "password must be one non-empty line"; password=raw.decode("utf-8"); hasher=PBKDF2PasswordHasher(); print(hasher.encode(password, hasher.salt()))' \
  | pbcopy
```

Edit only the encrypted document from the repository root, then clear the clipboard:

```sh
sops secrets/authentik.yaml
pbcopy </dev/null
```

The decrypted document has exactly these keys:

```yaml
authentik_secret_key: <random value>
authentik_postgres_password: <random value>
authentik_bootstrap_password_hash: <complete pbkdf2_sha256 value>
```

Pelargir renders exactly one Secret named `authentik-secrets` in namespace `authentik`,
with Kubernetes keys `secret-key`, `postgres-password`, and
`bootstrap-password-hash`. The rendered manifest remains mode 0400 under
`/run/secrets/rendered` (tmpfs) and is applied from there. Never copy it into the Nix
store, repository, or `/var/lib/rancher/k3s/server/manifests`.

`AUTHENTIK_BOOTSTRAP_PASSWORD_HASH` is read only on first successful worker bootstrap.
Changing it later does not change `akadmin`; use Authentik's account UI for later
password changes. Rotating `secret-key` is a migration, not routine hygiene: it can
invalidate or make cryptographic state unreadable. Restore the same value with the
database.

## Phase A: staged deployment

This procedure describes gates; it is not standing deployment authorization. For a new
installation or an isolated restore, first make a reviewed staging commit that sets all
three controllers to `replicas: 0`, `publish = false`, and both protection switches empty
or false. The accepted steady-state values at HEAD must not be deployed all at once to a
fresh database.

k3s applies the AddOn asynchronously after `nixos-rebuild` returns. A rollout command run
too early can inspect the previous zero-replica object and report success without creating
a Pod. After every Pelargir switch, first confirm the live `.spec.replicas` equals the
committed value, then gate on an actual Ready replica. The Authentik image entrypoint is
`dumb-init -- ak`; Kubernetes must supply `args: [worker]` or `args: [server]`. A
`command:` field replaces that entrypoint and fails with “executable file not found”.

### 1. Preflight

Before any rebuild, require all of the following:

- all YAML documents parse and every object is in namespace `authentik`;
- all three controller replica counts are zero;
- both images match their committed tags and digests;
- server and worker use `args`, with no Kubernetes `command` override;
- `secrets/authentik.yaml` reports encrypted SOPS MAC and age recipients;
- Pelargir and Minas evaluations succeed;
- the generated Authentik route and gate files both contain `http: {}`.

### 2. Install the inert foundation

Deploy Pelargir first. Its activation creates the namespace through
`minas-namespaces.yaml`, applies the rendered Secret from tmpfs, and installs the inert
AddOn. The filename sorts after the namespace manifest intentionally. Confirm without
printing Secret values:

```sh
sudo k3s kubectl get namespace authentik
sudo k3s kubectl -n authentik get secret authentik-secrets \
  -o go-template='{{range $key, $value := .data}}{{printf "%s\n" $key}}{{end}}' | sort
sudo k3s kubectl -n authentik get statefulset authentik-postgresql \
  -o jsonpath='{.spec.replicas}{"\n"}'
sudo k3s kubectl -n authentik get deployment authentik-worker authentik-server
sudo k3s kubectl -n kube-system get addon minas-workload-authentik
```

The key-name output must contain only the three names documented above; do not use
`describe`, `-o yaml`, shell tracing, or any output form that emits `.data` values.
All controller counts must be zero.

Deploy Minas next, still with every Authentik switch false. This installs backup and
monitoring logic plus two managed empty Traefik files. It hot-reloads the file provider;
it does not restart the singleton Traefik Pod or change an existing router.

### 3. Promote PostgreSQL alone

Change only the StatefulSet to `replicas: 1`, commit that state, deploy Pelargir, and
wait for the gate:

```sh
until [ "$(sudo k3s kubectl -n authentik get statefulset authentik-postgresql \
  -o jsonpath='{.spec.replicas}')" = 1 ]; do sleep 2; done
sudo k3s kubectl -n authentik wait --for=condition=Ready \
  pod/authentik-postgresql-0 --timeout=10m
sudo k3s kubectl -n authentik get pod authentik-postgresql-0 -o wide
sudo k3s kubectl -n authentik get pvc authentik-postgresql authentik-data
sudo k3s kubectl -n authentik logs authentik-postgresql-0 --tail=100
```

The PVCs must use `local-path-retain`. The installed manifest, live StatefulSet, Pod,
PVC, and AddOn revision must agree before continuing.

### 4. Promote the worker alone

In the next committed change, set only `authentik-worker` to `replicas: 1`, deploy
Pelargir, and verify migrations, health, and the custom blueprint:

```sh
until [ "$(sudo k3s kubectl -n authentik get deployment authentik-worker \
  -o jsonpath='{.spec.replicas}')" = 1 ]; do sleep 2; done
sudo k3s kubectl -n authentik wait \
  --for=jsonpath='{.status.readyReplicas}'=1 deployment/authentik-worker --timeout=15m
sudo k3s kubectl -n authentik exec deployment/authentik-worker -- ak healthcheck
sudo k3s kubectl -n authentik exec deployment/authentik-worker -- \
  ak migrate --check --noinput
sudo k3s kubectl -n authentik logs deployment/authentik-worker --tail=400 \
  | grep -iE 'blueprint|error|failed'
sudo k3s kubectl -n authentik exec deployment/authentik-worker -- \
  sha256sum /blueprints/custom/minas-admin-forward-auth.yaml
```

Do not proceed if the blueprint instance failed. A successful Kubernetes rollout alone
does not prove its objects were reconciled.

### 5. Promote the server alone

In the third committed promotion, set only `authentik-server` to `replicas: 1`, deploy
Pelargir, and verify both health endpoints:

```sh
until [ "$(sudo k3s kubectl -n authentik get deployment authentik-server \
  -o jsonpath='{.spec.replicas}')" = 1 ]; do sleep 2; done
sudo k3s kubectl -n authentik wait \
  --for=jsonpath='{.status.readyReplicas}'=1 deployment/authentik-server --timeout=10m
sudo k3s kubectl -n authentik get endpointslice \
  -l kubernetes.io/service-name=authentik-server
sudo k3s kubectl -n authentik exec deployment/authentik-server -- \
  ak healthcheck
sudo k3s kubectl -n authentik get pod -l app.kubernetes.io/component=server \
  -o jsonpath='{range .items[*]}{.metadata.name}{" ready="}{range .status.conditions[?(@.type=="Ready")]}{.status}{end}{"\n"}{end}'
```

The pinned image intentionally contains no `curl`/`wget`; do not write a runbook that
assumes either exists. The startup/readiness/liveness probes exercise the two HTTP health
endpoints in-container, while `ak healthcheck` and the Ready condition provide the
operator gate. The external endpoint checks below exercise HTTP once the route exists.

The Minas health check automatically creates
`/var/lib/healthcheck-ping/authentik.expected` only after all three named containers
have run together. From then on their disappearance is critical. Do not remove that
marker except during an explicitly authorized, fully detached foundation shutdown.

### 6. Publish Authentik itself

Only after internal readiness, set `authentikRollout.publish = true` and rebuild Minas.
This changes `k8s-authentik.yml` from `http: {}` to a route for
`authentik-server.authentik.svc.cluster.local:9000`. Leave `protectedRoutes` empty and
`protectDashboard = false`.

Verify from outside the LAN:

```sh
curl --fail --silent --show-error https://auth.saldivar.io/-/health/live/
curl --fail --silent --show-error https://auth.saldivar.io/-/health/ready/
curl --fail --silent --show-error \
  https://auth.saldivar.io/outpost.goauthentik.io/ping -o /dev/null
```

Also verify certificate identity/issuer and that no direct hostPort, NodePort, or bypass
path reaches Authentik.

### 7. Establish login and recovery

Sign in as `akadmin` with the chosen bootstrap password and confirm the bootstrap email.
In the User interface, open the upper-right gear, select **Credentials**, and enroll a
WebAuthn device. The `minas-passkey-first.yaml` blueprint links the packaged identification
stage to `default-authentication-mfa-validation`; Authentik's packaged policies then skip
both the password stage and duplicate MFA validation after a passwordless WebAuthn login.
Username/password remains available and an enrolled passkey is requested as its second
factor.

Before closing the original session, open a private browser, focus the username field, and
select the passkey from browser/password-manager autofill. If the password manager is not
enabled in private browsing, use a separate regular browser profile for this test. Also
prove the fallback path by entering username/password and completing its subsequent
passkey prompt.

Then establish recovery in another failure domain: enroll a second hardware passkey stored
separately and choose **Static tokens** under **Credentials** to generate six one-use codes.
Keep the codes outside the password-manager vault holding the primary passkey. Test the
second passkey and one static code in a separate browser before treating recovery as
accepted. This deployment has no SMTP-backed recovery flow; one primary passkey is
convenience, not recovery.

In Admin interface → Customization → Blueprints, confirm both “Minas Tirith -
passkey-first authentication” and “Minas Tirith - definite admin ForwardAuth applications”
show a successful latest application. Confirm the ten admin applications, one
`forward_single` provider and `authentik Admins` binding per app, and the managed embedded
outpost. Its provider set must be exactly those ten and both host fields must be
`https://auth.saldivar.io`.

### 8. Encrypted backup and scratch restore

After the monitoring latch exists, the nightly Minas backup creates:

- `k8s-authentik-authentik-postgresql.sql.gz.age`
- `k8s-authentik-data.tar.gz.age`

Both are streamed directly through age to the admin and Pelargir recipients. The live
PGDATA and `/data` PVC directories are excluded from the unencrypted rsync mirror, and
no plaintext Authentik dump is a valid restore target.

Before Phase A acceptance, trigger an authorized backup, decrypt both artifacts on a
machine holding an approved age identity, and restore them into disposable isolated
PostgreSQL/Authentik resources using the same pinned images and `secret-key`. Never point
a scratch server at the production Services or PVCs. Verify login, users, provider and
binding counts, outpost configuration, and `/data`; record hashes and results; then
remove only the explicitly disposable scratch resources.

Phase A is complete only after external login, independent recovery, encrypted backup,
and scratch restore all pass. Add Authentik to the public ingress acceptance baseline at
that point, not while it is intentionally unpublished.

## Phase B: accepted rollout and future additions

`authentikRollout.protectedRoutes` accepts only the declared admin candidates. The initial
Maintainerr canary chained `basic-auth@file` and `authentik-forward-auth@file`; its redirect,
passwordless WebAuthn login, callback, authorization, application API traffic, and rollback
shape were accepted from a real browser. The ARR and download-client waves then passed
external redirect, callback, workload readiness, endpoint, and Traefik error checks.

For a future candidate, commit and deploy one route first, then test:

- anonymous redirect and passkey login;
- access by an admin and denial for a non-admin;
- the `/outpost.goauthentik.io/` callback route;
- WebSocket/API behavior, logout, and native application login;
- external reachability and expected TLS identity.

Only after acceptance move to the next route. The active candidate set is Traefik,
Maintainerr, Sonarr, Radarr, Lidarr, Anime, Prowlarr, BT, BT Books, and Books DL. Traefik
is controlled by `protectDashboard` because its fallback dashboard router is hand-maintained;
the active generated override has higher priority and uses Authentik only.

Chaining BasicAuth during the canary intentionally meant two checks. It is detached in the
accepted steady state, so the edge experience is passkey/SSO only. Its middleware definition
remains available for emergency rollback without being attached to an active protected route.

Do not set an ARR application to `AuthenticationMethod=External` until every way to
reach it is proven to pass through Traefik. Initially keep native auth everywhere. User-
facing services such as Plex, Jellyfin, Overseerr, Nextcloud, Immich, Audiobookshelf,
Kavita, and Komga are outside this ForwardAuth plan.

## Forward-only rollback

For a failing application, remove only that route from `protectedRoutes`, rebuild Minas,
and verify its fallback. Maintainerr and Lidarr conditionally regain BasicAuth; the other
applications retain native login. For the dashboard, set `protectDashboard = false` to
restore the hand-maintained BasicAuth router. The renderer overwrites both route and gate
files, so disabled Authentik config cannot remain silently served as a stale file.

For a foundation failure, first detach ForwardAuth from every application and prove each
fallback; then set `publish = false`. If the workloads must stop, make consecutive
committed changes scaling server, worker, and PostgreSQL to zero in that order, verifying
each wave. Preserve the namespace, SOPS source, AddOn, Secret, both PVCs/PVs, monitoring
backups, and identity data. Only after every route is detached and all three declared
replica counts are zero may the operator remove
`/var/lib/healthcheck-ping/authentik.expected` to acknowledge an intentionally dormant
foundation; it will latch again after a future full promotion. Never remove the marker
to silence an unplanned outage. Rollback contains the failure; it does not delete
identity state.

Upgrade server and worker together and keep every Authentik component on one supported
version. Take and scratch-test a fresh encrypted backup first. A PostgreSQL major upgrade
is a separate maintenance operation with its own logical dump, restore proof, and
rollback plan.
