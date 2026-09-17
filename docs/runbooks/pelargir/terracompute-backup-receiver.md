# Pelargir Terracompute backup receiver

`hosts/nixos/pelargir/terracompute-backup-receiver.nix` declares a restricted
SFTP destination for encrypted Terracompute controller-state backups. The module
is disabled by default. Pelargir's host configuration enables it with the
dedicated Imladris sender public key; no deployment has occurred.

The receiver owns `/backups/terracompute-ops` through the system account
`terracompute-backup`. Its sshd match block chroots the account to `/backups`,
forces `internal-sftp` to start in `/terracompute-ops`, and disables passwords,
forwarding, tunnels, TTYs, and X11. Use a new
Imladris key; do not reuse Pelargir's host key, its Minas backup identity, or the
Terracompute target-observer key.

The restic client URL is
`sftp:terracompute-backup@pelargir:/terracompute-ops`. The client-visible path
is relative to the `/backups` chroot, so it maps to the host directory
`/backups/terracompute-ops`.

A root oneshot publishes `/backups/terracompute-preflight.json` every minute.
It reports the fixed machine/repository/commissioning identities, a 250 GiB
logical quota, and usable bytes capped by both remaining logical quota and a
100 GiB Pelargir filesystem reserve. The SFTP user can read the root-owned file
but cannot replace it. This is an admission guard, not an ext4 hard quota; monitor
backup age and repository size independently.

Before enabling:

1. Generate the dedicated sender key through the fleet's sops workflow and put
   only its public half in `authorizedKey`.
2. Pin Pelargir's SSH host key on Imladris without `accept-new`. The ED25519
   fingerprint observed directly on Pelargir on 2026-09-15 was
   `SHA256:RiqSHXqCwOJN6udIy7JgsWgSbeSJXm00QeVngbXg2Yc`; verify it again through
   an independent trusted path before commissioning.
3. Confirm Imladris resolves and reaches `pelargir` directly.
4. Build and inspect the evaluated sshd match block, user, directory modes,
   attestation unit, and timer.
5. Initialize the encrypted restic repository once, run a backup, and perform an
   isolated restore with manifest and SQLite verification.
6. Confirm the attestation becomes stale when its timer stops and that Imladris
   fails the backup before snapshot or restic execution.

The configured sender key fingerprint is
`SHA256:l4f4TuO3SF0Mr1cYyzERkJ7J4ykZiu3qfExUm45hHbI`. Recheck the evaluated key
before deployment and compare it with the retained public file on the operator
workstation.

Minas remains the later offsite copy. Give it a different account, key, host pin,
and repository so compromise or rotation of one receiver does not affect both.
