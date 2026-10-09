# Pelargir backup receiver

`hosts/nixos/pelargir/archive-backup-receiver.nix` declares a restricted SFTP
destination for one external sender's encrypted backups. The module is disabled
by default. Pelargir's host configuration enables it with the dedicated sender
public key. The sender itself is configured outside this repository.

The runtime names (`terracompute-backup`, `/backups/terracompute-ops`, the unit
names) predate the module's rename and are kept on purpose: changing them is a
migration, not a cleanup.

The repository is its own filesystem: `terracompute-backup-volume.service` loop-mounts
the sparse 250 GiB ext4 image `/var/lib/terracompute-backup/volume.img` over
`/backups/terracompute-ops` before sshd starts. That size is the hard quota: a
faulty or compromised sender can grow the repository to 250 GiB and no further.
The image is sparse, so that growth still comes out of Pelargir's root filesystem;
the reserve below keeps a cooperating sender from pushing it too far.
While unmounted the mountpoint is root-owned `0700`, so a push that races a failed
mount is refused rather than written to the root filesystem.

On its first start the unit ends any live `terracompute-backup` sessions, refuses
if the root filesystem cannot hold a copy plus the 100 GiB reserve, copies an
existing repository into the new image, verifies the copy, publishes the image,
and moves the original to `/backups/terracompute-ops.pre-volume`. Delete that copy
by hand once a restore from the volume has been verified. If the unit reports an
interrupted migration (`.pre-volume` present without `volume.img`, or data in both
places) it refuses to guess: finish or roll back the move by hand.

The mounted repository is owned by the system account `terracompute-backup`. Its sshd match block chroots the account to `/backups`,
forces `internal-sftp` to start in `/terracompute-ops`, and disables passwords,
forwarding, tunnels, TTYs, and X11. Use a new
sender key; do not reuse Pelargir's host key, its Minas backup identity, or any
other key the sender holds.

The restic client URL is
`sftp:terracompute-backup@pelargir:/terracompute-ops`. The client-visible path
is relative to the `/backups` chroot, so it maps to the host directory
`/backups/terracompute-ops`.

A root oneshot publishes `/backups/terracompute-preflight.json` every minute.
It reports the fixed machine/repository/commissioning identities, the 250 GiB
quota, and usable bytes capped by the volume's free space, the remaining quota,
and a 100 GiB reserve on Pelargir's root filesystem (which the sparse image grows
into). The SFTP user can read the root-owned file but cannot replace it. The
reserve is an admission guard for a cooperating sender; the hard limit is the
volume. Monitor backup age and repository size independently.

Before enabling:

1. Generate the dedicated sender key through the fleet's sops workflow and put
   only its public half in `authorizedKey`.
2. Pin Pelargir's SSH host key on the sender without `accept-new`. The ED25519
   fingerprint observed directly on Pelargir on 2026-09-15 was
   `SHA256:RiqSHXqCwOJN6udIy7JgsWgSbeSJXm00QeVngbXg2Yc`; verify it again through
   an independent trusted path before commissioning.
3. Confirm the sender resolves and reaches `pelargir` directly.
4. Build and inspect the evaluated sshd match block, user, directory modes,
   attestation unit, and timer.
5. Initialize the encrypted restic repository once, run a backup, and perform an
   isolated restore with manifest and SQLite verification.
6. Confirm the attestation becomes stale when its timer stops and that the sender
   fails the backup before snapshot or restic execution.

The configured sender key fingerprint is
`SHA256:l4f4TuO3SF0Mr1cYyzERkJ7J4ykZiu3qfExUm45hHbI`. Recheck the evaluated key
before deployment and compare it with the retained public file on the operator
workstation.

Minas remains the later offsite copy. Give it a different account, key, host pin,
and repository so compromise or rotation of one receiver does not affect both.
