# nextcloud — quiesced rollback artifacts, and how to restore from them

Built **2026-08-09T09:30:59Z**. This is the rollback that the nextcloud section of
`K3S-HANDOFF.md` requires to exist before `replicas: 1`, and that cross-review rejected
three earlier scopes for lacking.

⚠️ These artifacts are a **point-in-time from 2026-08-09T09:30:59Z**, not from the cutover
moment. Docker was restarted immediately after they were built, so anything written to
nextcloud after that timestamp is NOT in them. Re-take the complete set at the actual
cutover. The measured 2026-08-09 capture took **under four minutes**; do not invent a time
estimate for a later capture.

---

## The artifacts

| artifact | where | covers |
|---|---|---|
| ZFS snapshot | `storage2@nextcloud-quiesced-20260809T093059Z` | `/storage2/nextcloud/data` (1.5 TB) **and** `/storage2/nextcloud/db` (PGDATA) |
| app-tree archive | `/storage2/backup/nextcloud-precutover-20260809T093059Z/app-tree.tar.gz` (206 MB, 28245 entries) | `/usr/local/lib/docker-nextcloud`, including `config/config.php` |
| database dump | same directory, `nextcloud-db.dump` (12.7 MB, `pg_dump -Fc`) | logical copy of database `nextcloud-db`; it does **not** contain global roles |
| identity baseline | same directory, `identity-baseline.txt` | users, storage identities, per-storage counts/bytes, table count and filecache rows |
| retained-container record | same directory, `container-config.txt` | Docker container, image, label, network and routing configuration needed by rollback |
| checksums | same directory, `SHA256SUMS` | on-host authoritative integrity list for the files in that artifact set |

These three values are retained verbatim from the existing runbook:

```
d5ddf41586a07b9944991cc7710b770167dec8bc8f669714de6a0d2881df40e6  app-tree.tar.gz
392d13fd8ee88a58f93029b235108ae44ce8a4e7154f28566718f7a62d01aa80  nextcloud-db.dump
18cafb951e4b498a4c74a5259286117c808fdf8eb4cca5f49e014b0d855035c4  identity-baseline.txt
```

Do not copy a guessed checksum for `container-config.txt` into this document. The
on-host `SHA256SUMS` in the selected artifact directory is authoritative. Check the
whole file, not the three prose values above.

## ✅ Why this capture is coherent — the property the 08:40 snapshot lacked

The earlier `storage2@nextcloud-precutover-20260809T084029Z` was taken with all three
containers **running**, and its database dump was taken separately. It is torn and must
not be used. The corrected set was built in one quiesced window, in this order:

1. `occ maintenance:mode --on`, verified `maintenance: true`.
2. Docker app stopped, then redis; both were `Exited (0)`.
3. `pg_dump -Fc` completed while PostgreSQL still ran but the writers were absent.
4. `nextcloud-db` stopped cleanly in the measured **0.255 s**.
5. `postmaster.pid` was absent, the process and open-handle checks returned zero, and all
   three containers were `Exited (0)`.
6. One **atomic ZFS snapshot** captured data and PGDATA together; the stopped app tree was
   then archived from its separate `/dev/mapper/cr_root` filesystem.
7. The snapshot's `PG_VERSION` is 14. `pg_controldata` records `shut down`, checkpoint
   `4/275F04A0`, and system identifier `7147093535374221351`.

`pg_controldata` validates the control file's recorded state, checkpoint and system
identifier. It does **not**, by itself, prove an arbitrary non-atomic copy was untorn.
Coherence here follows from the whole chain: stopped database, absent process and open
handles, the atomic snapshot, and matching control data.

✅ The logical dump was restore-tested in an isolated PostgreSQL 14.5 cluster with
`--no-owner --no-acl`. That test had zero diagnostic lines and every captured identity
matched: 4 users including `edgar`/`Edgar`, 5 storage id strings and per-storage counts
and bytes, 103 tables, and 124415 filecache rows. The flags mattered: a single-database
`pg_dump -Fc` does **not** contain roles, so the test deliberately did not restore owners.

⚠️ The archive was created with
`tar --numeric-owner --acls --xattrs -C /usr/local/lib -czf -`. The source-tree
measurement found 0 non-trivial ACLs, 0 xattrs, 0 symlinks and 0 special nodes. ACL or
xattr loss is therefore moot for this measured tree; do not rewrite the capture history.

⚠️ The archived `config.php` contains `'maintenance' => true`. That seals the restored
application until every acceptance gate is complete. Maintenance mode is lifted only in
the final acceptance action below.

⚠️ Baseline per-storage `bytes` double-count because Nextcloud stores aggregated sizes
on directory rows. It is a deterministic fingerprint, not a disk-usage total.

## ⛔ What these artifacts do not protect against

They are on the same unencrypted pool as the live data. They do not protect against disk
failure, pool loss or loss of the building. They protect this migration rollback only.
`/storage2/nextcloud/data` still has no off-host copy.

The 2026-08-09 set also has a known acceptance gap: **it contains no hashes and fileids
for representative user files**. Counts, paths and database identities cannot prove the
bytes of a known file. The real-cutover capture gate below closes that gap for the next
set; it must not be claimed retroactively for this set.

---

## Before the real cutover — capture known-file identities

Do this while the old service is authoritative and before its quiesced snapshot. Store
the result beside the new artifact set; do not commit user paths, hashes or credentials.

The cutover operator must select representative, readable user files across every
storage that holds user content. Record, for each selection, the storage id, fileid,
Nextcloud path and SHA-256 of the bytes returned by an authenticated read. Also record:

- SHA-256 and path for `.ocdata`;
- the `appdata_*` directory's storage id, fileid and path;
- SHA-256 and path for `config/config.php`;
- the authenticated read's HTTP status and byte hash, but never its credential.

Use `oc_storages.numeric_id = oc_filecache.storage` to obtain the fileid. Use an
operator-supplied app credential read from its approved host location at runtime to read
the selected file through WebDAV, write the response to a mode-0600 temporary file, and
hash that file. A 2xx status without matching bytes is not acceptance. A hash without a
matching storage id/path/fileid is not acceptance. Delete the temporary response and
unset the credential when capture is complete.

Call the result `known-file-identities.txt`, add it to the new `SHA256SUMS`, and verify
that it has at least one user-file row for every user-content storage plus the three
special identities above. If any command fails or any required set is empty, the cutover
gate is NO-GO. The 2026-08-09T09:30:59Z artifact set predates this step and does **not**
contain this file.

At the real cutover, prepare a mode-0600 `known-file-selections.tsv` on the host with one
tab-separated row per representative user file: storage id, fileid, Nextcloud path and
its fully encoded WebDAV URL. Obtain the first three fields with a read-only query joining
`oc_storages.numeric_id` to `oc_filecache.storage`; review the query output and refuse an
empty storage. Then run this on **minas**, reading the app credential from its approved
host location into the hidden prompt:

```sh
set -euo pipefail
ART=/storage2/backup/nextcloud-precutover-REPLACE-WITH-CUTOVER-TIMESTAMP
SELECTED="$ART/known-file-selections.tsv"
KNOWN="$ART/known-file-identities.txt"
operator_uid=$(id -u)
operator_gid=$(id -g)
TMP=$(sudo mktemp /run/nextcloud-known-file.XXXXXX)
sudo chown "$operator_uid:$operator_gid" "$TMP"
sudo chmod 0600 "$TMP"
test -w "$TMP"
trap 'rm -f "$TMP"; unset nc_auth' EXIT
umask 077

test -s "$SELECTED"
printf 'Nextcloud user id: ' >&2
IFS= read -r nc_user
printf 'Read the approved app credential from the host, then paste it here: ' >&2
IFS= read -rs nc_auth
printf '\n' >&2
test -n "$nc_user"
test -n "$nc_auth"
: >"$KNOWN"

while IFS="$(printf '\t')" read -r storage fileid path url; do
  test -n "$storage" && test -n "$fileid" && test -n "$path" && test -n "$url"
  code=$(curl --fail-with-body --silent --show-error --location \
    --user "$nc_user:$nc_auth" --output "$TMP" --write-out '%{http_code}' "$url")
  case "$code" in 200|206) ;; *) printf 'authenticated read returned %s\n' "$code" >&2; exit 1;; esac
  test -s "$TMP"
  hash=$(sha256sum "$TMP" | awk '{print $1}')
  test -n "$hash"
  printf '%s\t%s\t%s\t%s\n' "$hash" "$storage" "$fileid" "$path" >>"$KNOWN"
done <"$SELECTED"

test -s "$KNOWN"
sha256sum /storage2/nextcloud/data/.ocdata \
  /usr/local/lib/docker-nextcloud/config/config.php >>"$KNOWN"
db_role=$(sudo docker inspect nextcloud-db --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | sed -n 's/^POSTGRES_USER=//p')
test -n "$db_role"
appdata=$(sudo docker exec nextcloud-db \
  psql -X -Aqt -v ON_ERROR_STOP=1 -U "$db_role" -d nextcloud-db -c \
  "SELECT 'APPDATA', s.id, f.fileid, f.path FROM oc_filecache f JOIN oc_storages s ON s.numeric_id=f.storage WHERE f.path ~ '^appdata_[^/]+$' ORDER BY s.id, f.fileid;")
test -n "$appdata"
printf '%s\n' "$appdata" >>"$KNOWN"
test "$(wc -l <"$KNOWN" | tr -d ' ')" -ge 4
sha256sum "$KNOWN" >>"$ART/SHA256SUMS"
unset nc_auth
rm -f "$TMP"
trap - EXIT
```

**GATE:** the operator must append and read the `appdata_*` identity query result, prove
that every user-content storage has a representative row, and run `sha256sum -c
SHA256SUMS`. The script cannot infer which storages are representative; an empty or
unreviewed selection is NO-GO.

---

## RESTORE

### ⛔ Never use `zfs rollback`

`/storage2/nextcloud` is a **directory inside the `storage2` root dataset**, not a dataset
of its own. A `zfs rollback` of this snapshot would revert the entire 2.83 TB root
dataset, including unrelated trees. Every restore below is selective and reads from:

```
/storage2/.zfs/snapshot/nextcloud-quiesced-20260809T093059Z/nextcloud/{data,db}
```

### Operator conventions

Commands labelled **pelargir** run in a shell on pelargir. Commands labelled **minas**
run in a shell on minas. Keep an operator shell open on each host and stop on any
non-zero status. Do not paste a block into the wrong host.

For every shell block, `set -euo pipefail` is load-bearing. An empty `grep` result is not
the same as a successful probe. Each emptiness check below first captures a complete,
non-empty tool report, then interprets the filter status separately.

### Step 0 — HARD PREREQUISITES: retained Docker objects and two-host rollback source

⛔ `docker start` is safe only if the retained containers, their pinned images, labels,
networks, mounts and the Docker Traefik router still exist. Never substitute
`docker compose up -d`: it recreates containers from today's compose state.

On **minas**, capture the current retained-object configuration and compare every object
against `container-config.txt` before changing anything:

```sh
set -euo pipefail
ART=/storage2/backup/nextcloud-precutover-20260809T093059Z
CHECK=/storage2/backup/nextcloud-rollback-prerequisite.$(date -u +%Y%m%dT%H%M%SZ).txt

command -v docker >/dev/null
sudo docker inspect nextcloud-db nextcloud nextcloud-redis traefik >"$CHECK"
test -s "$CHECK"
test -s "$ART/container-config.txt"
sudo docker network inspect nextcloud-net traefik-net >>"$CHECK"
test -s "$CHECK"
sudo docker ps -a --filter name=nextcloud --format '{{.Names}} {{.Image}} {{.Status}}' \
  | tee -a "$CHECK"
test -s "$CHECK"
```

**GATE:** read both files. Confirm the three retained Nextcloud containers, the Docker
Traefik container/router, required labels, images, networks and mounts match the recorded
rollback objects. If any are absent or ambiguous, stop. Recreating them is a separate
recovery plan, not permission to improvise here.

The declarative rollback spans both hosts:

- `hosts/nixos/pelargir/manifests.nix` delivers `minas-nextcloud.yaml` from the repository
  to pelargir's k3s auto-deploy directory.
- `hosts/nixos/minas-tirith/traefik-routes.nix` installs `k8s-nextcloud.yml` on minas.

Rebuilding or activating one host does not deploy the other. Prepare an authorized
rollback revision that sets **both** Deployment `replicas` fields in
`hosts/nixos/minas-tirith/manifests/nextcloud.yaml` to `0` and removes the `nextcloud`
route from `traefik-routes.nix`. Do not deploy the route change yet; Step 1 first makes
the workload incapable of returning.

### Step 1 — ⛔ CONTAIN the writer first, then neutralise k3s auto-deploy durably

⛔ **CONTAINMENT BEFORE DURABILITY.** The declarative procedure below is the correct fix,
but it costs a repository edit and **two full pelargir activations**. If a Pod is writing
to PGDATA *right now*, that is minutes of continued concurrent writes on the database you
are trying to save. Stop the writer first, then make it stick.

Run this on **pelargir** immediately, before anything else:

```sh
set -euo pipefail
NS=nextcloud
# Best-effort containment. NOT durable: auto-deploy can reassert these at any time,
# which is exactly why the declarative work below still has to happen.
sudo k3s kubectl -n "$NS" scale deploy/nextcloud    --replicas=0 || true
sudo k3s kubectl -n "$NS" scale deploy/nextcloud-db --replicas=0 || true
sudo k3s kubectl -n "$NS" wait --for=delete pod -l app=nextcloud    --timeout=120s || true
sudo k3s kubectl -n "$NS" wait --for=delete pod -l app=nextcloud-db --timeout=120s || true
sudo k3s kubectl -n "$NS" get pods
```

⚠️ The `|| true` above is deliberate and is the **only** place in this document where a
failure is tolerated. Containment is best-effort and must not abort before the durable fix
runs — a missing Deployment or an already-empty namespace is a fine outcome here. Every
gate from Step 1's declarative work onward fails closed, and none of them treat this
containment as evidence of anything.

⛔ Do **not** stop after this step. An imperative scale survives only until the next
checksum change or k3s restart, and stopping here is precisely the false sense of safety
that makes the durable work feel optional.

`strategy: Recreate` is not a cross-runtime interlock. k3s auto-deploy reapplies a
manifest when its checksum changes or the server restarts. An imperative scale can be
overwritten. The repository manifest must declare zero.

Apply this in two deliberate pelargir activations so the app stops before PostgreSQL:

1. In the authorized repository revision, set only the `nextcloud` app Deployment to
   `replicas: 0`, retain the database's current declaration, activate that revision on
   **pelargir**, and run the app gate below.
2. Set the `nextcloud-db` Deployment to `replicas: 0`, activate pelargir again, and run
   the database gate. The resulting repository and installed manifest must both declare
   zero for both Deployments. Do **not** use `kubectl scale` as a substitute.

For each activation, stage the authorized revision at pelargir's documented absolute
repository path, set `PHASE=app` for the first revision and `PHASE=database` for the
second, then run this **on pelargir**. The source-manifest checks happen before the switch,
and the installed-manifest and live-object checks below happen after it. A successful
`nixos-rebuild` message alone is not evidence that the intended revision was delivered.

```sh
set -euo pipefail
REPO=/home/edgar/nixos-config
MANIFEST="$REPO/hosts/nixos/minas-tirith/manifests/nextcloud.yaml"
: "${PHASE:?set PHASE to app, then database}"
test -s "$MANIFEST"

manifest_replica() {
  awk -v wanted="$1" '
    $0 == "---" { kind = ""; name = "" }
    $1 == "kind:" { kind = $2 }
    kind == "Deployment" && $1 == "name:" { name = $2 }
    kind == "Deployment" && name == wanted && $1 == "replicas:" {
      print $2
      found++
    }
    END { if (found != 1) exit 2 }
  ' "$MANIFEST"
}

app_declared=$(manifest_replica nextcloud)
db_declared=$(manifest_replica nextcloud-db)
test "$app_declared" = 0
case "$PHASE" in
  app)      test "$db_declared" = 1 ;;
  database) test "$db_declared" = 0 ;;
  *) printf 'invalid PHASE: %s\n' "$PHASE" >&2; exit 1 ;;
esac
printf 'authorized source declares nextcloud=%s nextcloud-db=%s\n' \
  "$app_declared" "$db_declared"

sudo nixos-rebuild dry-build --flake "$REPO#pelargir"
sudo nixos-rebuild switch --flake "$REPO#pelargir"
```

If the failed cutover has already removed or stopped the database Deployment, do not
raise it to 1 merely to satisfy the intermediate revision. Stop and prepare an
incident-specific authorized intermediate revision that preserves the already-safer
database state while first taking the app to zero. Never make the database live again
to make this procedure look linear.

After each activation, run on **pelargir**, first with `WORKLOAD=nextcloud`, then with
`WORKLOAD=nextcloud-db`:

```sh
set -euo pipefail
NS=nextcloud
: "${WORKLOAD:?set WORKLOAD to nextcloud, then nextcloud-db}"

command -v k3s >/dev/null
spec=$(sudo k3s kubectl -n "$NS" get deployment "$WORKLOAD" \
  -o jsonpath='{.spec.replicas}')
test "$spec" = 0
sudo k3s kubectl -n "$NS" wait --for=delete pod \
  -l "app=$WORKLOAD" --timeout=180s

pods=$(sudo k3s kubectl -n "$NS" get pods -l "app=$WORKLOAD" \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
test -z "$pods" || { printf '%s\n' "$pods" >&2; exit 1; }
printf 'verified zero pods for %s\n' "$WORKLOAD"
```

Then verify the installed auto-deploy file itself contains two zero declarations and the
live Deployment specs are both zero. Do not accept an empty report:

```sh
set -euo pipefail
INSTALLED=/var/lib/rancher/k3s/server/manifests/minas-nextcloud.yaml
sudo test -s "$INSTALLED"
count=$(sudo awk '$1 == "replicas:" && $2 == "0" { n++ } END { print n + 0 }' "$INSTALLED")
test "$count" -eq 2

live=$(sudo k3s kubectl -n nextcloud get deployment nextcloud nextcloud-db \
  -o custom-columns=NAME:.metadata.name,DESIRED:.spec.replicas --no-headers)
test -n "$live"
printf '%s\n' "$live"
test "$(printf '%s\n' "$live" | awk '$2 != 0 { n++ } END { print n + 0 }')" -eq 0
```

With auto-deploy safely pinned to zero, delete the live app Deployment, wait for its
deletion, then delete the database Deployment and wait. A future reconciliation may
recreate the Deployment objects, but the declared zero prevents Pods from returning:

```sh
set -euo pipefail
sudo k3s kubectl -n nextcloud delete deployment nextcloud --wait=true
sudo k3s kubectl -n nextcloud wait --for=delete deployment/nextcloud --timeout=60s
sudo k3s kubectl -n nextcloud delete deployment nextcloud-db --wait=true
sudo k3s kubectl -n nextcloud wait --for=delete deployment/nextcloud-db --timeout=60s

pods=$(sudo k3s kubectl -n nextcloud get pods \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
test -z "$pods" || { printf '%s\n' "$pods" >&2; exit 1; }
printf 'verified nextcloud namespace has zero pods\n'
```

Now activate the prepared `traefik-routes.nix` rollback on **minas**. Its activation only
reports stale generated routes, so explicitly remove exactly
`/home/edgar/git/docker/infra/traefik/k8s-nextcloud.yml` after confirming the declarative
route is absent, then verify the file is absent. This is the required order:
auto-deploy neutralised → Deployments deleted → k3s route deleted.

```sh
set -euo pipefail
REPO=/home/edgar/nixos-config
test -s "$REPO/hosts/nixos/minas-tirith/traefik-routes.nix"
if grep -Eq '^[[:space:]]*nextcloud[[:space:]]*=' \
  "$REPO/hosts/nixos/minas-tirith/traefik-routes.nix"; then
  printf 'authorized route source still declares nextcloud; refusing activation\n' >&2
  exit 1
elif test "$?" -ne 1; then
  exit 1
fi
sudo nixos-rebuild dry-build --flake "$REPO#minas-tirith"
sudo nixos-rebuild switch --flake "$REPO#minas-tirith"

ROUTE=/home/edgar/git/docker/infra/traefik/k8s-nextcloud.yml
test -e "$ROUTE"
sudo rm -- "$ROUTE"
test ! -e "$ROUTE"
printf 'verified k3s nextcloud route is absent\n'
```

### Step 2 — prove all writers and open handles are absent, fail closed

On **minas**, stop the retained Docker app before the database if either was restarted by
the failed cutover. Then produce complete `docker`, `ps` and `lsof` reports. `lsof` stderr
is captured separately and its exit status is checked; redirecting its errors into a
count would fail open.

```sh
set -euo pipefail
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
EVID=/storage2/backup/nextcloud-rollback-interlock-$RUN_ID
operator_uid=$(id -u)
operator_gid=$(id -g)
sudo install -d -m 0700 -o "$operator_uid" -g "$operator_gid" "$EVID"
test -w "$EVID"

sudo docker stop -t 120 nextcloud
test "$(sudo docker inspect -f '{{.State.Running}}' nextcloud)" = false
sudo docker stop -t 120 nextcloud-db
test "$(sudo docker inspect -f '{{.State.Running}}' nextcloud-db)" = false
sudo docker stop -t 60 nextcloud-redis
test "$(sudo docker inspect -f '{{.State.Running}}' nextcloud-redis)" = false

sudo docker ps -a --filter name=nextcloud --format '{{.Names}} {{.Status}}' \
  >"$EVID/docker.txt"
test -s "$EVID/docker.txt"
if awk '$1 ~ /^nextcloud(-db|-redis)?$/ && $2 != "Exited" { bad=1 } END { exit bad }' \
  "$EVID/docker.txt"; then :; else cat "$EVID/docker.txt" >&2; exit 1; fi

ps -eo pid=,user=,args= >"$EVID/processes.txt"
test -s "$EVID/processes.txt"
if grep -E 'nextcloud|/storage2/nextcloud|/usr/local/lib/docker-nextcloud' \
  "$EVID/processes.txt" >"$EVID/writer-matches.txt"; then
  cat "$EVID/writer-matches.txt" >&2; exit 1
elif test "$?" -ne 1; then
  exit 1
fi

command -v lsof >/dev/null
if ! sudo lsof -nP >"$EVID/lsof.txt" 2>"$EVID/lsof.stderr"; then
  cat "$EVID/lsof.stderr" >&2; exit 1
fi
test -s "$EVID/lsof.txt"
if grep -E '/storage2/nextcloud|/usr/local/lib/docker-nextcloud' \
  "$EVID/lsof.txt" >"$EVID/open-handles.txt"; then
  cat "$EVID/open-handles.txt" >&2; exit 1
elif test "$?" -ne 1; then
  exit 1
fi
printf 'verified zero matching processes and zero matching open handles\n'
```

**GATE:** do not continue unless the live k3s specs are zero, k3s Pods are absent, the
retained Docker containers are exited, and the fail-closed process and handle reports
show no writer. Preserve `$EVID` with the rollback evidence.

### Step 3 — snapshot the current failed-cutover state before destructive work

This safety snapshot makes the rollback itself selectively reversible. It is not
permission to roll the root dataset backward.

On **minas**:

```sh
set -euo pipefail
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
SAFETY="storage2@nextcloud-failed-cutover-pre-restore-$RUN_ID"
SNAPSHOT_REPORT=/storage2/backup/nextcloud-snapshots-before-$RUN_ID.txt

sudo zfs list -H -t snapshot -o name >"$SNAPSHOT_REPORT"
test -s "$SNAPSHOT_REPORT"
if grep -Fx "$SAFETY" "$SNAPSHOT_REPORT" >/dev/null; then
  printf 'refusing to reuse safety snapshot name: %s\n' "$SAFETY" >&2
  exit 1
elif test "$?" -ne 1; then
  exit 1
fi
sudo zfs snapshot "$SAFETY"
actual=$(sudo zfs list -H -t snapshot -o name "$SAFETY")
test "$actual" = "$SAFETY"
printf 'created safety snapshot %s; preserve this name in the incident record\n' "$SAFETY"
```

**GATE:** record the exact `$SAFETY` name. If snapshot creation or the exact-name lookup
fails, stop before app, PGDATA or data-directory changes.

### Step 4 — select and authenticate the restore artifacts

On **minas**, set `SNAP` and `ART` to the point in time chosen for **both** database and
data. Never mix a database from one time with files from another.

```sh
set -euo pipefail
SNAP=nextcloud-quiesced-20260809T093059Z
ART=/storage2/backup/nextcloud-precutover-20260809T093059Z
SNAPROOT="/storage2/.zfs/snapshot/$SNAP/nextcloud"
CHECKSUM_REPORT="$ART/rollback-checksums.$(date -u +%Y%m%dT%H%M%SZ).txt"

test -d "$SNAPROOT/db"
test -d "$SNAPROOT/data"
test -s "$ART/SHA256SUMS"
test ! -e "$CHECKSUM_REPORT"
if ! (cd "$ART" && sha256sum -c SHA256SUMS) >"$CHECKSUM_REPORT"; then
  cat "$CHECKSUM_REPORT" >&2
  exit 1
fi
test -s "$CHECKSUM_REPORT"
cat "$CHECKSUM_REPORT"
pg_version=$(sudo awk '{ gsub(/[[:space:]]/, ""); print }' "$SNAPROOT/db/PG_VERSION")
test "$pg_version" = 14
```

Read every checksum result. An empty checksum report is failure. The snapshot, archive,
dump and baselines selected here remain one consistency unit through the rest of the
procedure.

### Step 5 — restore the app tree without overwriting the current copy

The app tree is on `/dev/mapper/cr_root`, not ZFS. Stage, validate, preserve under a
unique name, then rename. `--numeric-owner` is required. The archive already contains
the measured-zero ACL/xattr/symlink/special-node state described above.

```sh
set -euo pipefail
RUN_ID=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
ART=/storage2/backup/nextcloud-precutover-20260809T093059Z
APP_STAGE="/usr/local/lib/nextcloud-app-restore-stage-$RUN_ID"
APP_KEEP="/usr/local/lib/docker-nextcloud.pre-restore-$RUN_ID"

test ! -e "$APP_STAGE"
test ! -e "$APP_KEEP"
sudo install -d -m 0700 "$APP_STAGE"
sudo tar --numeric-owner --acls --xattrs -xzf "$ART/app-tree.tar.gz" -C "$APP_STAGE"
sudo test -s "$APP_STAGE/docker-nextcloud/config/config.php"
sudo mv /usr/local/lib/docker-nextcloud "$APP_KEEP"
sudo mv "$APP_STAGE/docker-nextcloud" /usr/local/lib/docker-nextcloud
sudo rmdir "$APP_STAGE"
sudo test -s /usr/local/lib/docker-nextcloud/config/config.php
```

If either rename fails, stop. Do not extract over the live path. `$APP_KEEP` is the
reversible failed-cutover copy and must survive until acceptance.

### Step 6 — stage and validate PGDATA, then atomically exchange directories

#### Preferred path: physical PGDATA from the matching snapshot

⛔ Never copy old PGDATA into an existing directory. A failed preserve followed by an
`rsync` overlay mixes relation and WAL files from different database states.

On **minas**:

```sh
set -euo pipefail
RUN_ID=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
IMAGE='postgres@sha256:135c62a8134dcef829a1e4f5568bfae44bcfa2c75659ff948f43c71964366aa4'
SOURCE=/storage2/.zfs/snapshot/nextcloud-quiesced-20260809T093059Z/nextcloud/db
STAGE="/storage2/nextcloud/db.restore-stage-$RUN_ID"
KEEP="/storage2/nextcloud/db.pre-restore-$RUN_ID"
CONTROL="/storage2/backup/nextcloud-pg-control-$RUN_ID.txt"

test -d /storage2/nextcloud/db
test ! -e "$STAGE"
test ! -e "$KEEP"
sudo install -d -m 0700 "$STAGE"
sudo rsync -aHAX --numeric-ids --delete "$SOURCE/" "$STAGE/"
pg_version=$(sudo awk '{ gsub(/[[:space:]]/, ""); print }' "$STAGE/PG_VERSION")
test "$pg_version" = 14

sudo docker run --rm --network none --user 0 --entrypoint pg_controldata \
  -v "$STAGE:/d:ro" "$IMAGE" -D /d >"$CONTROL"
test -s "$CONTROL"
grep -Eq '^Database cluster state:[[:space:]]+shut down[[:space:]]*$' "$CONTROL"
grep -Eq '^Database system identifier:[[:space:]]+7147093535374221351[[:space:]]*$' "$CONTROL"
cat "$CONTROL"

sudo mv /storage2/nextcloud/db "$KEEP"
sudo mv "$STAGE" /storage2/nextcloud/db
sudo test -s /storage2/nextcloud/db/global/pg_control
```

A different system identifier means a freshly initialized or wrong cluster: abort. If
either rename fails, stop and use the recorded paths to recover; never fall back to an
in-place copy. Both renames are within the same filesystem and the final rename is the
atomic installation point.

#### Emergency fallback: rebuild PostgreSQL 14 from the logical dump

Use this only if the physical PGDATA is damaged. The dump is for one database and has no
global roles. The earlier restore test passed because it used `--no-owner --no-acl`; the
procedure below explicitly creates the required login role first and makes it own the
database and restored objects.

Read the role name and credential from the retained host configuration at runtime. The
commands below deliberately do not print either. If the retained object is unavailable,
stop and use the approved host credential location; never put a value in this repository.

On **minas**:

```sh
set -euo pipefail
RUN_ID=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
IMAGE='postgres@sha256:135c62a8134dcef829a1e4f5568bfae44bcfa2c75659ff948f43c71964366aa4'
ART=/storage2/backup/nextcloud-precutover-20260809T093059Z
STAGE="/storage2/nextcloud/db.restore-stage-$RUN_ID"
KEEP="/storage2/nextcloud/db.pre-restore-$RUN_ID"
TMP=nextcloud-db-logical-restore-$RUN_ID
LOG="/storage2/backup/nextcloud-pg-restore-$RUN_ID.log"
# /run is tmpfs, so the transient .pgpass below is memory-backed and never hits a disk.
PASSDIR="/run/nextcloud-restore-$RUN_ID"

test ! -e "$STAGE"
test ! -e "$KEEP"
test -s "$ART/nextcloud-db.dump"
db_role=$(sudo docker inspect nextcloud-db --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | sed -n 's/^POSTGRES_USER=//p')
db_auth=$(sudo docker inspect nextcloud-db --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | sed -n 's/^POSTGRES_PASSWORD=//p')
test -n "$db_role"
test -n "$db_auth"
test "$(printf '%s\n' "$db_role" | wc -l | tr -d ' ')" -eq 1
# The role and password are quoted into psql/.pgpass below. A value containing a quote or
# backslash would break that quoting silently, so refuse it rather than guess.
case "$db_role$db_auth" in *\'*|*\\*|*:*) printf 'credential contains a quoting metacharacter; handle by hand\n' >&2; exit 1 ;; esac

pg_uid=$(sudo docker run --rm --network none --entrypoint id "$IMAGE" -u postgres)
pg_gid=$(sudo docker run --rm --network none --entrypoint id "$IMAGE" -g postgres)
test -n "$pg_uid"
test -n "$pg_gid"
sudo install -d -m 0700 -o "$pg_uid" -g "$pg_gid" "$STAGE"
sudo docker run --rm --network none --user "$pg_uid:$pg_gid" \
  --entrypoint initdb -v "$STAGE:/d" "$IMAGE" \
  -D /d --auth-local=trust --auth-host=scram-sha-256
pg_version=$(sudo awk '{ gsub(/[[:space:]]/, ""); print }' "$STAGE/PG_VERSION")
test "$pg_version" = 14

sudo docker run -d --name "$TMP" --network nextcloud-net --user "$pg_uid:$pg_gid" \
  -v "$STAGE:/var/lib/postgresql/data" -v "$ART:/restore:ro" \
  "$IMAGE" postgres -c listen_addresses='*' >/dev/null
trap 'sudo docker rm -f "$TMP" >/dev/null 2>&1 || true; sudo rm -f "$PASSDIR/pgpass" >/dev/null 2>&1 || true; sudo rmdir "$PASSDIR" >/dev/null 2>&1 || true; unset db_auth' EXIT

for attempt in $(seq 1 60); do
  if ready_output=$(sudo docker exec "$TMP" pg_isready -U postgres -d postgres 2>&1); then
    ready=yes
    break
  else
    ready_status=$?
  fi
  printf 'pg_isready attempt %s exited %s: %s\n' \
    "$attempt" "$ready_status" "${ready_output:-<empty diagnostic>}" >&2
  case "$ready_status" in 1|2) ;; *) exit 1;; esac
  sleep 1
done
if test "${ready:-no}" != yes; then
  printf 'PostgreSQL did not become ready after 60 checked attempts\n' >&2
  exit 1
fi

# CREATE ROLE is explicit because pg_dump -Fc of one database contains no roles.
# ⛔ The password is fed on STDIN, never as an argument. A `-v role_password=...` argument
# is visible in `ps` on the host for the life of the command, to every local user.
sudo docker exec -i "$TMP" psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres <<SQL
\set role_name '$db_role'
\set role_password '$db_auth'
CREATE ROLE :"role_name" LOGIN PASSWORD :'role_password';
CREATE DATABASE "nextcloud-db" OWNER :"role_name";
SQL

if ! sudo docker exec "$TMP" pg_restore --exit-on-error --no-owner --no-acl \
  --role="$db_role" -U postgres -d nextcloud-db /restore/nextcloud-db.dump \
  >"$LOG" 2>&1; then
  cat "$LOG" >&2
  exit 1
fi
test -e "$LOG"
if test -s "$LOG"; then
  cat "$LOG" >&2
  printf 'pg_restore emitted diagnostics; review every line and stop\n' >&2
  exit 1
fi

sudo docker exec -i "$TMP" psql -X -v ON_ERROR_STOP=1 -U postgres -d nextcloud-db \
  -v role_name="$db_role" <<'SQL'
ALTER DATABASE "nextcloud-db" OWNER TO :"role_name";
ALTER SCHEMA public OWNER TO :"role_name";
SQL

owners=$(sudo docker exec "$TMP" psql -X -Aqt -v ON_ERROR_STOP=1 -U postgres \
  -d nextcloud-db -v expected="$db_role" -c \
  "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname !~ '^pg_toast' AND r.rolname <> :'expected';")
test "$owners" = 0

# Prove a separate container on the retained app network can authenticate over TCP.
# ⛔ NOT `-e PGPASSWORD=`: a container environment variable is readable for the life of the
# container via `docker inspect`, and this fleet already records that env-borne secrets land
# on disk in container metadata. Use a mode-0600 .pgpass on /run, which is tmpfs (memory
# backed), mounted read-only — the same "render secrets to tmpfs" pattern used elsewhere here.
sudo install -d -m 0700 "$PASSDIR"
# printf is a shell builtin, so the password never becomes a visible process argument.
printf '%s:5432:%s:%s:%s\n' "$TMP" nextcloud-db "$db_role" "$db_auth" \
  | sudo tee "$PASSDIR/pgpass" >/dev/null
sudo chmod 0600 "$PASSDIR/pgpass"
tcp_result=$(sudo docker run --rm --network nextcloud-net \
  -v "$PASSDIR/pgpass:/pgpass:ro" -e PGPASSFILE=/pgpass \
  --entrypoint psql "$IMAGE" \
  -X -Aqt -v ON_ERROR_STOP=1 -h "$TMP" -U "$db_role" \
  -d nextcloud-db -c 'SELECT 1;')
sudo rm -f "$PASSDIR/pgpass"
sudo rmdir "$PASSDIR"
test "$tcp_result" = 1
sudo docker exec "$TMP" pg_isready -h 127.0.0.1 -U "$db_role" -d nextcloud-db
sudo docker stop -t 120 "$TMP" >/dev/null
sudo docker rm "$TMP" >/dev/null
trap - EXIT
unset db_auth

CONTROL="/storage2/backup/nextcloud-logical-pg-control-$RUN_ID.txt"
sudo docker run --rm --network none --user 0 --entrypoint pg_controldata \
  -v "$STAGE:/d:ro" "$IMAGE" -D /d >"$CONTROL"
test -s "$CONTROL"
grep -Eq '^Database cluster state:[[:space:]]+shut down[[:space:]]*$' "$CONTROL"

sudo mv /storage2/nextcloud/db "$KEEP"
sudo mv "$STAGE" /storage2/nextcloud/db
sudo test -s /storage2/nextcloud/db/global/pg_control
```

The logical rebuild necessarily has a new system identifier, so the physical-capture
identifier gate does not apply to this fallback. `--auth-host=scram-sha-256` is
load-bearing: the retained container entrypoint does not rewrite `pg_hba.conf` when it
finds pre-initialized PGDATA. `--auth-host=reject` would let the local restore pass and
then reject the app container's TCP connection. The gates here are successful
initialization, explicit role/database creation, zero `pg_restore` diagnostics,
ownership checks, an authenticated TCP query, clean shutdown, and the identity
comparison in Step 9.

### Step 7 — compare the entire data directory, then choose the point-in-time action

Database and data are one consistency unit. A database restored to 09:30:59Z cannot be
accepted with later file bytes merely because the failed cutover probably made no writes.

⚠️ **Triage first — the authoritative pass is expensive.** The checksumming comparison
below reads all 1.5 TB on **both** sides, and nextcloud stays sealed in maintenance mode
for its whole duration. No timing is asserted here, but plan for it in hours, not minutes,
and do not start it without knowing which answer you need.

Run the **fast** comparison first. It uses rsync's default size+mtime heuristic, so it is
bounded by metadata rather than by reading 1.5 TB:

```sh
set -euo pipefail
RUN_ID=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
SOURCE=/storage2/.zfs/snapshot/nextcloud-quiesced-20260809T093059Z/nextcloud/data
TRIAGE="/storage2/backup/nextcloud-data-triage-$RUN_ID.txt"

test -d "$SOURCE"
test -d /storage2/nextcloud/data
test ! -e "$TRIAGE"
if ! sudo rsync -naxHAX --numeric-ids --delete --itemize-changes \
  "$SOURCE/" /storage2/nextcloud/data/ >"$TRIAGE"; then
  printf 'triage comparison failed; it proves nothing: %s\n' "$TRIAGE" >&2
  exit 1
fi
printf 'triage: %s differing paths in %s\n' "$(wc -l <"$TRIAGE" | tr -d ' ')" "$TRIAGE"
```

- **Non-empty** → the data directory definitely changed. You already know you need the
  point-in-time restore; skip straight to it and let the post-restore verification below
  be the authoritative check. Running the slow pass first only delays the fix.
- **Empty** → the trees match on size, mtime and metadata, which is *suggestive but not
  proof*: a same-size, same-mtime content change passes it. Proceed to the authoritative
  pass below before accepting "unchanged" as the basis for skipping the restore.

⛔ An empty triage result is NOT sufficient grounds to skip the copy. Only the checksumming
pass can establish that, and skipping the copy is a decision about 1.5 TB of user data.

Now produce the **complete checksumming dry run**. It is not truncated and no pipeline
can hide `rsync` failure.

On **minas**:

```sh
set -euo pipefail
RUN_ID=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
SOURCE=/storage2/.zfs/snapshot/nextcloud-quiesced-20260809T093059Z/nextcloud/data
PLAN="/storage2/backup/nextcloud-data-dry-run-$RUN_ID.txt"

test -d "$SOURCE"
test -d /storage2/nextcloud/data
if ! sudo rsync -nacHAX --numeric-ids --delete --itemize-changes \
  "$SOURCE/" /storage2/nextcloud/data/ >"$PLAN"; then
  printf 'data dry run failed; plan is incomplete: %s\n' "$PLAN" >&2
  exit 1
fi
test -e "$PLAN"
lines=$(wc -l <"$PLAN" | tr -d ' ')
printf 'complete itemised data dry run: %s lines in %s\n' "$lines" "$PLAN"
sudo sed -n '1,$p' "$PLAN"
```

**GATE:** the operator must read the entire `$PLAN`, record its line count and sign the
decision in the incident record.

- If the successful checksum comparison has exactly zero itemised lines, record that the
  data directory is byte/metadata-identical to the selected snapshot and skip the copy.
  Zero is accepted only because `rsync` itself succeeded and the complete plan file and
  explicit line count were recorded.
- If there is even one line, the live data changed. To keep the selected database point,
  perform the explicit restore below. The Step 3 safety snapshot preserves the later
  failed-cutover state for selective recovery.

⛔ **DESTRUCTIVE POINT-IN-TIME RESTORE — `--delete` removes every live file absent from
the selected snapshot, including post-snapshot uploads.** Run this only after consciously
accepting that exact point in time and recording the safety snapshot name and dry-run
plan. A dry run is not a restore.

```sh
set -euo pipefail
: "${SAFETY:?record the Step 3 safety snapshot name in this shell}"
: "${PLAN:?record the complete Step 7 dry-run path in this shell}"
test -e "$PLAN"
printf 'Type RESTORE-SELECTED-POINT to delete live-only files: ' >&2
IFS= read -r decision
test "$decision" = RESTORE-SELECTED-POINT

SOURCE=/storage2/.zfs/snapshot/nextcloud-quiesced-20260809T093059Z/nextcloud/data
sudo rsync -acHAX --numeric-ids --delete --itemize-changes \
  "$SOURCE/" /storage2/nextcloud/data/ \
  >"${PLAN%.txt}.applied.txt"
test -e "${PLAN%.txt}.applied.txt"
printf 'data restore completed; preserve %s and %s\n' \
  "$SAFETY" "${PLAN%.txt}.applied.txt"
```

Re-run the complete checksumming dry run after the copy. It must succeed with an explicit
zero line count before continuing:

```sh
set -euo pipefail
RUN_ID=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
SOURCE=/storage2/.zfs/snapshot/nextcloud-quiesced-20260809T093059Z/nextcloud/data
POST_PLAN="/storage2/backup/nextcloud-data-post-restore-$RUN_ID.txt"

test ! -e "$POST_PLAN"
if ! sudo rsync -nacHAX --numeric-ids --delete --itemize-changes \
  "$SOURCE/" /storage2/nextcloud/data/ >"$POST_PLAN"; then
  printf 'post-restore data comparison failed: %s\n' "$POST_PLAN" >&2
  exit 1
fi
test -e "$POST_PLAN"
if test -s "$POST_PLAN"; then
  cat "$POST_PLAN" >&2
  printf 'post-restore comparison is non-empty; data still differs\n' >&2
  exit 1
fi
printf 'post-restore comparison succeeded with explicit zero lines: %s\n' "$POST_PLAN"
```

### Step 8 — start retained Docker under the maintenance seal

Before starting, verify again that the k3s route file is absent, both k3s workloads have
no Pods, and the retained Docker configuration still matches `container-config.txt`.
Start only the database, require database readiness, then start and verify the retained
Redis container before starting the retained app. Although the captured configuration
did not use Redis for caching or file locking, Step 2 stopped this retained dependency;
leaving it stopped is not a valid restoration of the retained Docker state. Do not
expose the service through a recreated compose container.

First run this again **on pelargir**, immediately before the Docker start. This closes the
window in which a bad activation or k3s restart could have reasserted a Pod:

```sh
set -euo pipefail
INSTALLED=/var/lib/rancher/k3s/server/manifests/minas-nextcloud.yaml
sudo test -s "$INSTALLED"
count=$(sudo awk '$1 == "replicas:" && $2 == "0" { n++ } END { print n + 0 }' "$INSTALLED")
test "$count" -eq 2

live=$(sudo k3s kubectl -n nextcloud get deployment \
  -o custom-columns=NAME:.metadata.name,DESIRED:.spec.replicas --no-headers)
for workload in nextcloud nextcloud-db; do
  replicas=$(printf '%s\n' "$live" | awk -v wanted="$workload" \
    '$1 == wanted { print $2; found++ } END { if (found > 1) exit 2 }')
  case "$replicas" in
    '') printf '%s Deployment is absent (zero live specs)\n' "$workload" ;;
    0)  printf '%s Deployment has live desired replicas 0\n' "$workload" ;;
    *)  printf '%s Deployment has unsafe desired replicas %s\n' \
          "$workload" "$replicas" >&2; exit 1 ;;
  esac
done

pods=$(sudo k3s kubectl -n nextcloud get pods \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
test -z "$pods" || { printf '%s\n' "$pods" >&2; exit 1; }
printf 'pre-start k3s interlock still holds: declared zero, live zero, zero pods\n'
```

Then **on minas**, re-capture the retained objects and compare the complete report to
`container-config.txt`. Do not proceed merely because `docker inspect` exited zero:

```sh
set -euo pipefail
ART=/storage2/backup/nextcloud-precutover-20260809T093059Z
RUN_ID=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
NOW="/storage2/backup/nextcloud-container-pre-start-$RUN_ID.txt"

test -s "$ART/container-config.txt"
test ! -e "$NOW"
sudo docker inspect nextcloud-db nextcloud nextcloud-redis traefik >"$NOW"
sudo docker network inspect nextcloud-net traefik-net >>"$NOW"
test -s "$NOW"
printf 'compare every retained object in %s with %s before starting Docker\n' \
  "$NOW" "$ART/container-config.txt"
```

**GATE:** the operator must read the complete pre-start report and re-confirm the pinned
images, labels, networks, mounts and Docker router. Missing, changed or unreviewed output
is NO-GO.

On **minas**:

```sh
set -euo pipefail
test ! -e /home/edgar/git/docker/infra/traefik/k8s-nextcloud.yml
db_role=$(sudo docker inspect nextcloud-db --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | sed -n 's/^POSTGRES_USER=//p')
test -n "$db_role"
sudo docker start nextcloud-db >/dev/null

for attempt in $(seq 1 60); do
  if ready_output=$(sudo docker exec nextcloud-db \
    pg_isready -U "$db_role" -d nextcloud-db 2>&1); then
    ready=yes
    break
  else
    ready_status=$?
  fi
  printf 'pg_isready attempt %s exited %s: %s\n' \
    "$attempt" "$ready_status" "${ready_output:-<empty diagnostic>}" >&2
  case "$ready_status" in 1|2) ;; *) exit 1;; esac
  sleep 1
done
if test "${ready:-no}" != yes; then
  printf 'PostgreSQL did not become ready after 60 checked attempts\n' >&2
  exit 1
fi

sudo docker start nextcloud-redis >/dev/null
for attempt in $(seq 1 60); do
  if redis_reply=$(sudo docker exec nextcloud-redis redis-cli ping 2>&1); then
    if test "$redis_reply" != PONG; then
      printf 'redis-cli succeeded with unexpected output: %s\n' \
        "${redis_reply:-<empty output>}" >&2
      exit 1
    fi
    redis_ready=yes
    break
  else
    redis_status=$?
  fi
  printf 'redis-cli attempt %s exited %s: %s\n' \
    "$attempt" "$redis_status" "${redis_reply:-<empty diagnostic>}" >&2
  sleep 1
done
if test "${redis_ready:-no}" != yes; then
  printf 'Redis did not become ready after 60 checked attempts\n' >&2
  exit 1
fi
test "$(sudo docker inspect -f '{{.State.Running}}' nextcloud-redis)" = true

sudo docker start nextcloud >/dev/null
state=$(sudo docker exec -u www-data nextcloud php occ maintenance:mode)
test -n "$state"
printf '%s\n' "$state"
printf '%s\n' "$state" | grep -q 'enabled'
```

The database role is read from the retained host configuration; it is never written into
this repository. `pg_isready` alone is not acceptance; the next step runs database-backed
application and identity checks.

### Step 9 — identity verification, content and authenticated-read verification

Keep maintenance mode enabled. Counts alone do not identify the database, and
`occ files:scan --all` is forbidden because it mutates what is being checked.

Run the same read-only SQL/`occ` capture commands that produced
`identity-baseline.txt`, save their complete output to a new restore report, and compare
it with the selected baseline. Check all of these, not just totals:

- user ids and display names;
- storage id strings;
- per-storage counts and aggregated-byte fingerprints;
- 103 tables and 124415 filecache rows for the 2026-08-09 set.

The comparison itself is fail closed:

```sh
set -euo pipefail
ART=/storage2/backup/nextcloud-precutover-20260809T093059Z
: "${RESTORE_IDENTITIES:?path to the complete newly captured identity report}"
test -s "$ART/identity-baseline.txt"
test -s "$RESTORE_IDENTITIES"
if ! cmp -s "$ART/identity-baseline.txt" "$RESTORE_IDENTITIES"; then
  diff -u "$ART/identity-baseline.txt" "$RESTORE_IDENTITIES" >&2 || true
  exit 1
fi
sha256sum "$ART/identity-baseline.txt" "$RESTORE_IDENTITIES"
```

Then verify `.ocdata`, the `appdata_*` directory identity and `config/config.php`. For a
real-cutover artifact set, re-query every selected storage id/path/fileid, hash the same
bytes, and compare every row to `known-file-identities.txt`. Perform the authenticated
read through the sealed application context and compare its byte hash. A safe way to keep
maintenance mode on is to run the read from the Nextcloud CLI context as `www-data`, using
the operator-supplied user credential from its approved host location and the selected
file identity; do not print or store the credential. If that prepared authenticated
read mechanism is absent, this gate is NO-GO — do not lift maintenance mode to improvise
an exposed WebDAV test.

For the prepared check, choose one known user-file row, set `NC_USER` to its owner,
`NC_PATH` to its path relative to that user's files root, and `EXPECTED_SHA` to the
captured hash. Read the app credential from its approved host location at runtime. This
uses Nextcloud's own password check and filesystem abstraction from a maintenance-safe
CLI context; it does not turn maintenance mode off:

```sh
set -euo pipefail
: "${NC_USER:?set the owner from the selected known-file row}"
: "${NC_PATH:?set the path relative to the selected user files root}"
: "${EXPECTED_SHA:?set the captured sha256 for that exact row}"
printf '%s\n' "$EXPECTED_SHA" | grep -Eq '^[0-9a-f]{64}$'
operator_uid=$(id -u)
operator_gid=$(id -g)
TMP=$(sudo mktemp /run/nextcloud-authenticated-read.XXXXXX)
sudo chown "$operator_uid:$operator_gid" "$TMP"
sudo chmod 0600 "$TMP"
test -w "$TMP"
trap 'rm -f "$TMP"; unset nc_auth' EXIT
printf 'Read the approved app credential from the host, then paste it here: ' >&2
IFS= read -rs nc_auth
printf '\n' >&2
test -n "$nc_auth"

sudo docker exec -e nc_user="$NC_USER" -e nc_auth="$nc_auth" -e nc_path="$NC_PATH" \
  -u www-data nextcloud php -r '
define("OC_CONSOLE", true);
require_once "/var/www/html/lib/base.php";
$user = OC::$server->getUserManager()->checkPassword(getenv("nc_user"), getenv("nc_auth"));
if ($user === false) { fwrite(STDERR, "authentication failed\n"); exit(20); }
$node = OC::$server->getUserFolder($user->getUID())->get(getenv("nc_path"));
$stream = $node->fopen("r");
if ($stream === false) { fwrite(STDERR, "read failed\n"); exit(21); }
stream_copy_to_stream($stream, STDOUT);
' >"$TMP"
test -s "$TMP"
actual=$(sha256sum "$TMP" | awk '{print $1}')
test "$actual" = "$EXPECTED_SHA"
printf 'authenticated known-file read matched sha256 %s\n' "$actual"
unset nc_auth
rm -f "$TMP"
trap - EXIT
```

The 2026-08-09 set has no `known-file-identities.txt`, so it cannot pass the known-byte
part of this gate. State that gap explicitly in the incident decision; do not claim that
`identity-baseline.txt` proves file bytes. The operator must choose a separately approved
risk disposition before this historical set can be exposed.

**GATE:** preserve the complete comparison output. Empty output is not a pass. Every
captured identity and every available known-byte row must match, and the authenticated
read must return the expected bytes, before continuing.

While maintenance is still enabled, verify that the retained Docker Traefik router is
present and is the only router prepared to serve `drive.saldivar.io`. Do not use an
unauthenticated 302 as an application gate.

### Step 10 — FINAL acceptance action: lift maintenance mode and expose Docker routing

Only after Steps 0–9 and the historical known-byte-gap disposition have passed:

```sh
set -euo pipefail
before=$(sudo docker exec -u www-data nextcloud php occ maintenance:mode)
test -n "$before"
printf '%s\n' "$before" | grep -q 'enabled'

# FINAL WRITE/EXPOSURE ACTION. Stop here if any earlier gate is incomplete.
sudo docker exec -u www-data nextcloud php occ maintenance:mode --off
after=$(sudo docker exec -u www-data nextcloud php occ maintenance:mode)
test -n "$after"
printf '%s\n' "$after" | grep -q 'disabled'
```

There is no acceptance step after this block. If the mode-off command or its immediate
state check fails, re-enable maintenance mode and stop; do not accept new writes.

---

## Lifecycle — preserve rollback-of-rollback until acceptance

The selected quiesced snapshot and the Step 3 safety snapshot grow as live blocks change.
Do not destroy either during the incident. Record both names and their role. Destruction
is a later, separately approved lifecycle action only after acceptance and the soak
period. Never turn cleanup into a root-dataset rollback.
