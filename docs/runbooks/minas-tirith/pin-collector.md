# PinCollector Phase 1 on k3s

`pin-collector-release.nix` declares separate `staged` and `enabled` gates. Staging
creates the retained data services; enabling unsuspends migration/bootstrap and declares
one API replica and one model replica.

## Declared shape

- Namespace `pin-collector`, Restricted Pod Security.
- API Deployment at one replica on minas-tirith.
- pgvector PostgreSQL StatefulSet with a `local-path-retain` PVC.
- Garage (S3 object store) StatefulSet in its own auto-deploy file,
  `minas-pin-collector-garage.yaml`; see `pin-collector-garage.md`.
- Triton/model Deployment on minas-tirith with `runtimeClassName: nvidia`, one accounted
  `nvidia.com/gpu` request, a retained model cache, and no public Service.
- An API-image-digest-versioned migration Job; API startup migrations and seed ingestion are off.
- `send-notifications`, a CronJob on minas-tirith every 15 minutes in the API image
  (`python -m app.maintenance.send_notifications`), suspended unless the release sets
  `notificationSender`. With no APNs settings it sends nothing and exits 0, but still
  expires and prunes notification events, so it runs before the APNs key exists.
- `release-import`, a CronJob on minas-tirith in the API image
  (`python -m app.maintenance.release_import --write`), suspended unless the release sets
  `releaseImport`. It runs at 10:00 UTC, which is 02:00 or 03:00 Pacific, can start up to 10
  minutes late and has 1h50 to run, so it ends by 05:00 Pacific. Requests 384Mi with a 1536Mi limit
  and mounts the database URL, the Garage app key and secret, and the reader's key.
- ClusterIP Services only. The minas Traefik file provider owns `pin.saldivar.io`; admin is
  `/admin` on the same origin.

The device plugin advertises three time-slices for Plex, Jellyfin, and PinCollector. This
only lets the scheduler account for all three; it does not increase VRAM, throughput, or
fault isolation.

## Secrets

`secrets/pin-collector.yaml` is encrypted to the admin and pelargir recipients only.
Sops-nix renders each value to its own tmpfs file on pelargir.
`k3s-apply-secrets` passes those files to `kubectl create secret --from-file` and pipes the
generated object directly to `kubectl apply`, preserving arbitrary bytes without inserting
them into YAML, command arguments, logs, the Nix store, or persistent disk. The worker node
cannot decrypt the SOPS document.

Application secrets are mounted as files, with each workload receiving only the keys it
needs. Garage's admin token is limited to Garage and its bootstrap Job; the API uses a
separate app key restricted to `pin-collector-uploads`. Do not
replace file mounts with `secretKeyRef` env values: resolved env values persist in
containerd metadata. Store the read-only package token
as compact Docker config JSON under `ghcr_dockerconfigjson`. Provision or replace it
through the hidden-input helper so it never enters a plaintext temporary file or shell
argument. Set `registryPullSecretReady = true` only after that encrypted key exists.

## Enabling a release

After the PinCollector manual publish workflow completes, verify its API fingerprint and
both OCI revision labels match the reviewed commit, then set:

- `gitRevision` to the reviewed 40-character lowercase Git commit;
- `apiImage` and `modelImage` to the workflow's `ghcr.io/...@sha256:...` outputs;
- `apiImageRevision` and `modelImageRevision` to the independently inspected
  `org.opencontainers.image.revision` labels; both must equal `gitRevision`;
- `registryPullSecretReady = true` only after the encrypted key exists;
- `staged = true` to create retained PVCs and start only PostgreSQL and Garage;
- `enabled = true` only after the old PostgreSQL and object data has been restored under a
  separately authorized cutover. This unsuspends the migration Job and raises API/model
  replicas from zero to one.

When GitHub Actions minutes are spent, the images are published by hand on nardol with
buildx (linux/amd64, provenance and SBOM). First run the workflow's test gate locally on
the reviewed commit: backend `pytest tests` and `ruff check app tests`, and model-service
`pytest tests`. The API is built from `git archive` of that commit with
`--build-arg PIN_COLLECTOR_BUILD_SHA=<full sha>` and tagged `:<full sha>`, as the workflow
does. The model image may instead be relabelled from the live model digest, same layers
with only its revision label changed, when
`git diff OLD..NEW -- model-service seed-data .dockerignore` is empty: its requirements are
ranges, so a rebuild could pull untested torch or transformers versions. Verify both OCI
revision labels and the API's baked fingerprint from the published images either way.

Feature gates, each optional and off when absent. Set one in the release that first pins
an image carrying the feature, never earlier: the config is useless or harmful to an older
image.

- `notificationSender = true` (PinCollector 9e894cd and later, Wave 3) unsuspends the
  `send-notifications` CronJob.
- `collectorPages = true` (Wave 4, PinCollector PR #132) mounts `collector-link-key` into
  the API and sets `PIN_COLLECTOR_COLLECTOR_LINK_KEY_FILE` and `PIN_COLLECTOR_GET_APP_URL`.
  That changes the API Pod template, so the API restarts with the release. The "Get the
  app" link points at `/support` until a TestFlight invite exists. The link key is in
  `secrets/pin-collector.yaml` as `collector_link_key`; replacing it makes every existing
  collector link show as null until its owner makes a new one.
- `stardust = true` and `plus = true` (PinCollector b237cd9 and later, Wave 2) set
  `PIN_COLLECTOR_STARDUST_ENABLED` and `PIN_COLLECTOR_PLUS_ENABLED` in the API ConfigMap.
  A ConfigMap edit restarts nothing, so the renderer also names the switches that are on in
  the API Pod's `pin-collector.saldivar.io/feature-gates` annotation: flipping either
  restarts the API with the release. `plus_beta_open` keeps the app's default (true), so
  while it is on every signed-in account has Plus. Turn stardust on only after the
  conversion below. Neither gate renames the migration Job.

- `suggestDetailsKeys = true` (PinCollector Step 3 of Suggest details, PR #251, and later) declares
  `serpapi_api_key`, `searchapi_api_key` (PinCollector #279 and later) and `detail_reader_api_key`
  from `secrets/pin-collector.yaml`, adds them to `pin-collector-runtime` as `serpapi-api-key`,
  `searchapi-api-key` and `detail-reader-api-key`, mounts them into the API (and the stardust
  conversion Job, which mounts what the API does), and sets `PIN_COLLECTOR_SERPAPI_API_KEY_FILE`,
  `PIN_COLLECTOR_SEARCHAPI_API_KEY_FILE` and `PIN_COLLECTOR_DETAIL_READER_API_KEY_FILE`. The new
  items change the API Pod template, so the API restarts with the release; the migration Job keeps
  its name. All three keys must be in the SOPS document first, because sops-nix validates it at
  build time:

  ```sh
  sops secrets/pin-collector.yaml      # add serpapi_api_key:, searchapi_api_key: and detail_reader_api_key:, save
  sops -d --extract '["serpapi_api_key"]' secrets/pin-collector.yaml | wc -c        # a length, not the value
  sops -d --extract '["searchapi_api_key"]' secrets/pin-collector.yaml | wc -c
  sops -d --extract '["detail_reader_api_key"]' secrets/pin-collector.yaml | wc -c
  ```

  The reader key is a dedicated key for the reader's gateway at `inference.orleans.io`, not one
  shared with other clients. The keys alone turn nothing on: outside lookups start only when an
  admin chooses a provider (SearchApi or SerpApi, the other as fallback) and Qwen at
  `/admin/ui/suggestions`, and the app shows them only to collectors who agree. SearchApi searches
  only by a short-lived link to the picture, so it also needs the API's `PIN_COLLECTOR_PUBLIC_API_BASE_URL`. No NetworkPolicy limits the API's egress (the namespace's policies are
  ingress only), so it can reach `serpapi.com`, `www.searchapi.io` and `inference.orleans.io` on 443 as it does Resend
  and Apple. `inference.orleans.io` resolves to the home network's public address, so from a Pod it
  depends on the router's NAT loopback, as the coredns-custom notes describe for `saldivar.io`.

- `accountMail = true` (PinCollector's password reset codes) declares `resend_api_key` from
  `secrets/pin-collector.yaml`, adds it to `pin-collector-runtime` as `resend-api-key`, mounts it into
  the API (and the stardust conversion Job), and sets `PIN_COLLECTOR_RESEND_API_KEY_FILE` (on the
  API image's entrypoint allowlist) and `PIN_COLLECTOR_MAIL_FROM: "PinStellation
  <no-reply@pinstellation.com>"`. The app's default sender, `no-reply@saldivar.io`, isn't verified in
  Resend. The key has Sending access, limited to `pinstellation.com`. Only the API sends mail, so the
  nightly release import gets neither. The API restarts with the release; the migration Job keeps its
  name. The key must be in the SOPS document first:

  ```sh
  sops secrets/pin-collector.yaml      # add resend_api_key:, save
  sops -d --extract '["resend_api_key"]' secrets/pin-collector.yaml | wc -c          # a length, not the value
  ```

- `appleSignInKey = true` with `appleKeyId = "<Key ID>"` (Sign in with Apple's private key) declares
  `apple_sign_in_key` (the .p8, whole), adds it as `apple-sign-in-key`, mounts it into the API and
  sets `PIN_COLLECTOR_APPLE_TEAM_ID: N449SAELDJ`, `PIN_COLLECTOR_APPLE_KEY_ID` and
  `PIN_COLLECTOR_APPLE_PRIVATE_KEY_FILE`, a path the app reads itself. With all three the server keeps
  Apple refresh tokens and revokes them when an account is deleted. Sign-in works without them. The
  release contract refuses the gate without a 10-character Key ID. Add the key first:

  ```sh
  sops secrets/pin-collector.yaml      # add apple_sign_in_key: | with the .p8's lines, save
  sops -d --extract '["apple_sign_in_key"]' secrets/pin-collector.yaml | head -1   # -----BEGIN PRIVATE KEY-----
  ```

- `releaseImport = true` (PinCollector's monthly release sheets, PRs #257, #259 and #261, and later)
  unsuspends the `release-import` CronJob. It mounts the reader's key, so the release contract
  refuses it without `suggestDetailsKeys = true`. Neither gate renames the migration Job. Turning
  it on takes two separate steps, both the owner's:

  1. A release whose image contains those PRs sets `releaseImport = true`. That deploy also runs
     migrations `20261109_release01` to `20261109_release03`. From then on the job reads only the
     sheets an admin uploaded and left waiting on the Releases page.
  2. The fetch has its own switch at `/admin/ui/releases`, off by default. While it is off the job
     never touches the feed. The source is `PIN_COLLECTOR_RELEASE_FEED_SOURCE` (`disney_pins_blog`), set
     in both `pin-collector-api-config` (the admin's "Run import now", PinCollector #272) and
     `pin-collector-release-import-config` (the nightly job). Change it in both together.

  Sheets have their own reader (PinCollector #275, #276): `PIN_COLLECTOR_RELEASE_READER_BASE_URL`
  (`http://100.66.174.88:8000/v1`, nardol's inference server at its Tailscale address, no key) and
  `PIN_COLLECTOR_RELEASE_READER_MODEL` (`default`, the alias nardol's llama-server reports), in the same two
  ConfigMaps. Suggest details still uses `inference.orleans.io`. The Pods run on minas-tirith, where `10.0.0.118` is
  the other site's LAN, not nardol (minas routes all of 10.0.0.0/20 out its own eth0), so they reach nardol only over
  Tailscale. Port 8000 answers there because Docker publishes it past nardol's firewall, which opens it only on eth0
  (`hosts/nixos/nardol/inference.nix`); opening it on `tailscale0` too would make that explicit. If nardol is asleep,
  off, gaming, or serving a profile without `mmproj`, a read falls back to Tesseract and says why on the sheet. Delete
  both pairs to send sheets back to `inference.orleans.io`.

  Disney Pins Blog's terms ban scraping without permission. The owner has asked it by email, and
  turning the fetch on before it answers is the owner's call. If it stays silent, WDWNT's feed may
  stand in. If it says no, stop and ask the owner (PinCollector
  `docs/adr/0019-monthly-release-sheets.md`).

  A run killed mid-read leaves its sheet `reading`. The next run puts it back to wait, and if that
  read dies too the sheet is marked failed, which fails that run, and an admin can read it again.

### The stardust conversion

`stardustConversion = "dry-run"`, `"apply"` or `"refresh"` appends a one-shot Job,
`pin-collector-stardust-<mode>-<API digest>-<spec hash>`, and a NetworkPolicy admitting it
to PostgreSQL. It runs `python -m app.maintenance.convert_stardust` (plus `--apply` or
`--refresh`) in the API image with the API's ConfigMap and secret files, after waiting for
the schema to reach the image's Alembic head. It mounts every secret file the API does, on
purpose: it runs with the API's ConfigMap, and the entrypoint refuses a configured secret
file it cannot read. It never retries (`backoffLimit: 0`) and gets an hour. Re-rendering
the same release gives the same Job, so it runs once. A new mode, a new API image, an edit
to the Job's template, or a `collectorPages` flip renames it, and so runs it again. That is
harmless: a dry run writes nothing, `--apply` skips accounts it already converted, and
`--refresh` is idempotent. Removing the field prunes the Job, its Pod and its log. The
contract requires an enabled release and rejects `apply` or `refresh` once
`stardust = true`: the command refuses then too, because a conversion would get round the
daily limits.

Four deploys, each its own merged PR and each with the owner's go for that run:

1. `stardustConversion = "dry-run"`, stardust and Plus off. It writes nothing. Its log is
   one line of JSON: accounts seen, skipped and converted, total stardust, how many start at
   each level, the spread of starting stardust, and hand-recorded trades (max, p99, over
   50). Give that line to the owner before the next deploy, which prunes the Job and its log.
2. With the owner's OK on those numbers, `stardustConversion = "apply"`. Its report says
   `"applied": true`.
3. Right before switch-on, `stardustConversion = "refresh"`: it pays the history and the
   accounts added since the apply.
4. Once the refresh Job is Complete and its report has been read,
   `stardust = true; plus = true;` with `stardustConversion` removed. This switch-on goes
   out with the iOS release that shows stardust and Plus.

Read a run on pelargir:

```sh
sudo -n k3s kubectl -n pin-collector get jobs | grep stardust
sudo -n k3s kubectl -n pin-collector logs job/<name>
sudo -n k3s kubectl -n pin-collector logs job/<name> -c wait-for-migration-head
```

A failed run stays failed, and redeploying the same release does not recreate it. Read its
log and fix the cause. Then, with the owner's go, delete the Job; k3s recreates it at its
next apply of the file (a deploy that changes the file, or a k3s server restart).

## Rollback

Setting `staged = false` keeps the frozen manifest under management but renders both
StatefulSets and both Deployments at zero replicas with the migration Job suspended. This
preserves PVCs and prevents k3s from continuing to reconcile a stale release. Changing
declared release gates is a deployment action and requires separate authority.

The Nix assertions reject an enabled release without staging, or a staged release with a
missing registry gate, mutable image, wrong repository, malformed revision, or image
revision that differs from the reviewed commit. Rebuild pelargir first so namespace,
Secrets, PVCs, workloads, and migration land before minas publishes the Traefik route.

## Acceptance

Require the migration Job complete (and a declared stardust conversion Job with it, its
report read), all four workloads healthy, `/health` and `/ready` at 200, `/admin`
redirecting to the session-aware `/admin/ui` entry point (and an unauthenticated
`/admin/ui` redirecting to `/admin/ui/login`), the exact two image IDs,
both OCI revision labels and workload annotations matching `gitRevision`, the API
fingerprint reporting that revision, the expected Alembic head, restored row/object
inventories, and a real GPU recognition while Plex and Jellyfin remain scheduled. Preserve
legacy Compose volumes and retained PVCs until a restore drill is accepted. Rollback,
restart, deployment, and production data mutation each remain separately authorized
actions.
