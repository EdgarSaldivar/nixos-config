# pelargir — off-host backup to minas

`backup.nix` declares the restic repository and nightly timer; minas'
`backup-receiver.nix` declares the restricted account and dataset guard. Both ends
are declarative, but **one-time initialisation and a proven restore are not** —
that is what this file is for.

Salvaged from MINAS-PREP.md on 2026-08-16. `backup-receiver.nix` supersedes that
file's imperative `useradd` steps; the procedure below is what it does not cover.

## Initialise the repository and prove a restore

**Edgar on pelargir:** after the SSH probe reaches the intended directory, run
the generated restic wrapper. It supplies repository/password configuration
without printing either secret.

```bash
set -euo pipefail
# Pin minas' host key on first contact. The backup preflight and restic's own
# sftp both run BatchMode=yes, which fails closed on an unknown host — and an
# unpinned first connection is also the one moment this transport could be
# redirected without notice.
sudo ssh -o StrictHostKeyChecking=accept-new \
  -i /etc/ssh/ssh_host_ed25519_key \
  pelargir-backup@minas-tirith true || true
# An uninitialized repository makes `restic snapshots` fail by design. Reach
# the restricted account with the same identity first, then initialize once.
sudo sftp -b /dev/null \
  -i /etc/ssh/ssh_host_ed25519_key \
  pelargir-backup@minas-tirith
sudo restic-minas init
sudo systemctl start restic-backups-minas.service
sudo restic-minas snapshots
restore_dir="$(mktemp -d)"
sudo restic-minas restore latest --target "$restore_dir" \
  --include /var/lib/restic-staging/pelargir/home-assistant-config/configuration.yaml
sudo test -f "$restore_dir/var/lib/restic-staging/pelargir/home-assistant-config/configuration.yaml"
printf 'Test restore retained at %s; inspect it, then remove it manually.\n' "$restore_dir"
```

⚠️ The host-key pin on first contact is not ceremony. The backup preflight and
restic's own sftp both run `BatchMode=yes`, which fails closed on an unknown host —
and an unpinned first connection is the one moment this transport could be
redirected without notice.

⏳ **A successful restore has not been observed since these were declared.** Until
`restic-minas snapshots` lists a recent snapshot and a scratch restore returns a
real file, the offsite backup is configured, not working.
