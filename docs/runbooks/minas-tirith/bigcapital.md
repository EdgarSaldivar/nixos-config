# Bigcapital (accounting.saldivar.io)

Bigcapital is double-entry accounting and inventory. One deployment holds any number of
businesses: each organization gets its own MariaDB database (`bigcapital_tenant_<id>`).

Every command runs on pelargir as `K='sudo -n k3s kubectl -n bigcapital'`. Never print
Secret data: no `describe secret`, no `-o yaml` on a Secret, no shell tracing.

## Declared shape

- `hosts/nixos/minas-tirith/manifests/bigcapital.yaml`, delivered by pelargir as the frozen
  AddOn `minas-bigcapital.yaml`. Everything is pinned to minas-tirith.
- `server` (API, port 3000) and `webapp` (static SPA, port 80), both `v0.25.45`.
- `mariadb` StatefulSet, MariaDB 11.4 LTS, on a 20Gi `local-path-retain` claim. The
  application user `bigcapital` holds `ALL ON bigcapital\_%.*` (the system database and
  every tenant database), not upstream's global grant.
- `redis` (BullMQ queues, append-only file on a 1Gi claim) and `gotenberg` (PDFs).
- `garage` StatefulSet (S3 for attachments) with bucket `bigcapital-files`, a read+write
  key for the server and a read-only key for backups. A separate instance from
  PinCollector's.
- Hash-named Jobs `garage-bootstrap-<hash>` and `migrate-<hash>`; see the header of
  `bigcapital.yaml`. `checks/bigcapital-contract.nix` prints the expected name when one
  is stale.
- Default-deny NetworkPolicies. The only egress beyond the namespace is the S3 hairpin to
  Traefik, SMTPS (port 465) from the server and HTTPS (443) from the backup, both to public
  addresses only. Baseline Pod Security enforced,
  Restricted audited.
- Mail through Resend's SMTP relay as `Accounting <accounting@saldivar.io>`, once the key
  exists (see Mail below).
- CronJob `backup`, nightly at 03:45 UTC **on pelargir**: every `bigcapital_%` database and
  every attachment, into a restic repository in the owner's house (see Backup and restore).
- MariaDB, Garage and their claims carry `objectset.rio.cattle.io/prune: "false"`.

### The edge

| path on `accounting.saldivar.io` | backend | gate |
|---|---|---|
| `/api…` | `server` | Authentik, then Bigcapital's login |
| `/bigcapital-files/…` | `garage` | S3 signature only |
| everything else | `webapp` | Authentik |

The attachment path cannot be gated. Bigcapital has one S3 client: it uploads through it
and it signs the download links it hands the browser, so its endpoint must be this public
origin, and the server's own requests carry no Authentik session. Every request there needs
a signature from a key only the server holds, download links expire after 300 seconds, and
only the one bucket's path is routed.

Authentik admits `authentik Admins` and the `Accounting users` group. Its provider does not
intercept the `Authorization` header, which Bigcapital's SPA uses for its own token.

## Secrets

`secrets/bigcapital.yaml`, encrypted to the admin and pelargir. Pelargir's
`k3s-apply-secrets` builds Secret `bigcapital-runtime` from the individual tmpfs files.
Keys: `app_jwt_secret`, `db_password`, `db_root_password`, `garage_rpc_secret`,
`garage_admin_token`, `garage_app_key_id`/`garage_app_secret`,
`garage_backup_key_id`/`garage_backup_secret` (Garage needs `GK` + 24 hex characters and
64 hex characters), `signup_allowed_emails`, and, optionally, `mail_password`.
`secrets/bigcapital-backup.yaml` holds `restic_password` and, optionally, `healthchecks_url`.
`db_password` must stay a hex string: the backup refuses anything else rather than quote it.

The key list lives in `hosts/nixos/pelargir/secrets/bigcapital-keys.nix`, shared by the sops
declarations and the applier. `mail_password` is declared only once the file contains it:
sops-nix fails pelargir's activation for a declared key the file lacks.

Bigcapital reads its configuration only from environment variables, so these reach the
containers as `secretKeyRef` env.

## Deploy

1. Merge, then switch **pelargir** from `origin/master`. It delivers the namespace, the
   Secret, the AddOn, the Authentik blueprint and CoreDNS. Expect one self-healing
   `namespaces "bigcapital" not found` on the first apply, and possibly exit status 4
   from the switch (namespace and Secret in one commit; AGENTS.md §1).
2. Wait for the data services, then the Jobs:

   ```sh
   $K get secret bigcapital-runtime \
     -o go-template='{{range $key, $value := .data}}{{printf "%s\n" $key}}{{end}}' | sort
   $K get pods,pvc
   $K wait --for=condition=complete job -l app=bigcapital-garage-bootstrap --timeout=10m
   $K logs -l app=bigcapital-garage-bootstrap --tail=20
   $K wait --for=condition=complete job -l app=bigcapital-migrate --timeout=10m
   $K logs -l app=bigcapital-migrate --tail=40
   $K wait --for=condition=Available deployment/server deployment/webapp --timeout=10m
   ```

   The Secret listing must show exactly the ten required keys above, plus `mail-password`
   once it exists. The bootstrap log must end with
   `app key read/write/delete, backup key list/read only: proven through S3`. Every claim
   must be `Bound` on `local-path-retain`.
3. In Authentik → Customization → Blueprints, confirm the admin ForwardAuth blueprint
   applied, the Bigcapital provider is on the embedded outpost, and the `Accounting users`
   group exists.
4. Switch **minas** from `origin/master`: the three routes and `/etc/hosts`.
5. Before DNS exists, test through the real ingress from the LAN:

   ```sh
   curl -sI --resolve accounting.saldivar.io:443:10.0.1.6 https://accounting.saldivar.io/
   curl -sI --resolve accounting.saldivar.io:443:10.0.1.6 https://accounting.saldivar.io/api/system_db
   curl -sI --resolve accounting.saldivar.io:443:10.0.1.6 https://accounting.saldivar.io/bigcapital-files/x
   ```

   The first two must answer `302` to `auth.saldivar.io`; the third must answer `403` from
   Garage (unsigned), never `302` and never the SPA.
6. Owner: create the Cloudflare record like the other minas names, a CNAME `accounting` to
   minas' public name, DNS only (grey cloud).
7. Sign in through Authentik, then use **Sign up** with an address from
   `signup_allowed_emails`. Each signup creates a new organization. Then, in that
   organization: attach a file to an invoice and download it again (proves the S3 path end
   to end), and print an invoice to PDF (proves Gotenberg).
8. In a separate change, add `accounting.saldivar.io  302  auth.saldivar.io` to the ingress
   baseline. Adding it before DNS resolves pages after about 15 minutes.

## Businesses, logins and users

- **One login belongs to one organization.** Bigcapital has no organization switcher
  (upstream issue #411), and an email address can hold only one login across the whole
  install. A second business needs a second address; plus-addresses work.
- **Signup is closed except for the allow-list.** `SIGNUP_DISABLED` is `"true"` on purpose:
  Bigcapital checks `SIGNUP_ALLOWED_EMAILS` only when signup is disabled, and matches exact,
  case-sensitive addresses. To allow another owner login, edit `signup_allowed_emails` with
  `sops secrets/bigcapital.yaml`, merge, switch pelargir, then restart the server so it reads
  the new value: `$K rollout restart deployment/server`.
- **A login's address can change later.** Preferences → Users → edit the user. The change
  reaches the login table. The allow-list is consulted only at signup.
- **Adding people to an organization** is Preferences → Users → Invite. The accept link is
  sent only by email, so invites need `mail_password` (below). An invited person also needs
  an Authentik account in the `Accounting users` group.

## Mail (Resend)

The server sends through `smtp.resend.com:465` (implicit TLS) as user `resend`, with a
Resend API key as the password, from `Accounting <accounting@saldivar.io>`. The sender is
global: both businesses send from it. `saldivar.io`'s inbound mail stays on iCloud; nothing
here touches its MX or apex SPF.

Until `mail_password` exists, the server starts normally and every send fails. To turn
mail on:

1. Resend → Domains → Add `saldivar.io`. Add exactly the records it lists in Cloudflare (a
   DKIM TXT under `_domainkey`, and the `send` subdomain's MX and SPF TXT), DNS only, then
   wait for Resend to show the domain verified. Explicit records take precedence over the
   zone's wildcard.
2. Resend → API Keys → Create, permission **Sending access**, domain `saldivar.io` only.
   Never reuse PinCollector's key: it is scoped to `pinstellation.com`.
3. Add it from the repository root, in your own terminal:

   ```sh
   nix run nixpkgs#ssh-to-age -- -private-key -i ~/.ssh/id_ed25519 -o /tmp/age.key
   SOPS_AGE_KEY_FILE=/tmp/age.key sops secrets/bigcapital.yaml
   shred -u /tmp/age.key
   ```

   In the editor add one line, `mail_password: <the re_… key>`, save, and clear the
   clipboard. Run `nix flake check`, open a PR, merge.
4. Switch pelargir from `origin/master`. The applier adds `mail-password` to the Secret; the
   server reads environment only at start, so then run `$K rollout restart deployment/server`.
5. Test with Preferences → Users → Invite to an address you can read, and confirm the
   message's headers show `dkim=pass` for `saldivar.io`.

## Upgrade

1. Read the upstream release notes between the pinned tag and the target.
2. Change the `bigcapitalhq/server` and `bigcapitalhq/webapp` references in
   `bigcapital.yaml` to the new tag and its index digest, everywhere. They must stay on the
   same tag.
3. Run `nix flake check`. `bigcapital-contract` fails and names the new `migrate-…` and
   `garage-bootstrap-…` names; rename both groups to exactly those names.
4. Deploy pelargir. k3s prunes the old Jobs and runs the new ones; the server is
   `Recreate`, so the old and new versions never run together.

## Backup and restore

### What runs

CronJob `bigcapital/backup`, 03:45 UTC, one pod on pelargir, stages in order:

1. `lock`: a sidecar holding `flock()` on the target for the pod's life, so a manual run
   can never overlap a scheduled one. Every stage refuses unless it reports `acquired`, and
   unless `/backup` is the directory `pelargir/bigcapital-backup.nix` prepared (sentinel).
2. `copy-1`: empty `staging/`, then copy the bucket with the **read-only** Garage key.
3. `dump`: `mariadb-dump --single-transaction` of every `bigcapital_%` database, found at
   run time, each verified complete; then list the attachment keys each organization's
   `documents` table references.
4. `copy-2`: copy again, for uploads made during the dump.
5. `check`: record how many referenced attachments are not in the copy. It never blocks the
   snapshot: the databases are saved even if an attachment is lost.
6. `restic`: snapshot `staging/` into `/var/lib/bigcapital-backup/restic`, tag it
   `complete`, keep 14 daily, 8 weekly, 24 monthly and 7 yearly; `restic check` nightly, a
   full `--read-data` on the 1st.
7. `finish`: empty `staging/` again, then ping Healthchecks: success, or `/fail` if any
   attachment was missing.

Between runs pelargir holds only the encrypted repository. A failed run can leave plaintext
in `staging/` until the next run starts.

### Turn on the alert (owner)

Until `healthchecks_url` exists, nobody is told when a backup fails; `finish` logs that.

1. healthchecks.io → new check `Bigcapital backup`, cron `45 3 * * *`, time zone UTC, grace
   2 hours.
2. Add `healthchecks_url: https://hc-ping.com/<uuid>` with
   `SOPS_AGE_KEY_FILE=/tmp/age.key sops secrets/bigcapital-backup.yaml` (the same age key
   procedure as Mail), merge, switch pelargir.

### First run, and any time

```sh
job=backup-manual-$(date -u +%s)
$K create job --from=cronjob/backup "$job"
$K wait --for=condition=complete "job/$job" --timeout=60m
for c in dump check restic finish; do $K logs "job/$job" -c "$c" --tail=20; done
```

`dump` names every database and the number of referenced attachments; `check` must report
`0 missing`. On pelargir, list the snapshots:

```sh
restic_bin=$(nix build --no-link --print-out-paths nixpkgs#restic)/bin/restic
R="sudo $restic_bin -r /var/lib/bigcapital-backup/restic --password-file /run/secrets/bigcapital_backup_restic_password"
$R snapshots --tag complete
```

### Restore

The repository password is `restic_password` in `secrets/bigcapital-backup.yaml`; with
pelargir gone it is the only way into a copy of the repository.

1. Stop the app in git: commit `replicas: 0` on Deployment `server`, merge, switch pelargir.
2. On pelargir, restore a snapshot to a scratch directory. It is plaintext: delete it after.

   ```sh
   $R restore <snapshot-id> --target /var/tmp/bigcapital-restore
   S=/var/tmp/bigcapital-restore/backup/staging
   ```

3. Databases. Each dump recreates its database and replaces its tables:

   ```sh
   for f in "$S"/db/bigcapital_*.sql.gz; do
     sudo zcat "$f" | sudo k3s kubectl -n bigcapital exec -i mariadb-0 -- \
       sh -c 'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" mariadb -uroot'
   done
   ```

4. Attachments, through the public bucket route with the server's read+write key (no
   NetworkPolicy admits an ad-hoc pod to Garage):

   ```sh
   rclone_bin=$(nix build --no-link --print-out-paths nixpkgs#rclone)/bin/rclone
   sudo env RCLONE_S3_PROVIDER=Other RCLONE_S3_REGION=garage RCLONE_S3_FORCE_PATH_STYLE=true \
     RCLONE_S3_ENDPOINT=https://accounting.saldivar.io \
     RCLONE_S3_ACCESS_KEY_ID="$(sudo cat /run/secrets/bigcapital_garage_app_key_id)" \
     RCLONE_S3_SECRET_ACCESS_KEY="$(sudo cat /run/secrets/bigcapital_garage_app_secret)" \
     "$rclone_bin" copy "$S/mirror" :s3:bigcapital-files
   ```

5. `sudo rm -rf /var/tmp/bigcapital-restore`, then commit `replicas: 1` again and switch
   pelargir.

Rehearse this once against a scratch copy before the books matter: an untested restore is
a hope, not a backup.

## Rotation

- `db_password` and `db_root_password` are read by MariaDB **only on first
  initialisation**. Changing them in sops alone locks the app out. First change the password
  inside MariaDB (`ALTER USER`), then sops, then switch pelargir and restart `server`.
- `app_jwt_secret`: rotating it signs everyone out. Switch pelargir, restart `server`.
- Garage keys: the bootstrap refuses an existing key whose secret differs from the declared
  one. Rotate by importing a new key ID, not by changing a secret in place.

## Known limits

- Gotenberg is 7.10.2, the major upstream tests against, and no longer maintained. Its
  network policy confines it to the server in both directions.
- When the Authentik session expires, the SPA's API calls are redirected and fail; reload
  the page.
- `ALLOW_EMPTY_DATADIR` on MariaDB's `verify-datadir` init container is `"true"` so the
  first start can initialise. Once `bigcapital_system` exists (deploy step 2), set it to
  `"false"` in its own commit, so a lost or replaced claim refuses to start instead of
  starting an empty ledger. Never set it before the first start: that start would refuse.
