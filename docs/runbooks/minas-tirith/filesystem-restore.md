# minas-tirith — filesystem restore

`backup-root-data` considers seven filesystem roots every night and skips any that
are absent or empty. This is how the captured roots go back. **Databases are NOT
restored this way** — see
[backup-restore.md](backup-restore.md), which covers the dump/restore procedure and
why `pg_dumpall` is not a valid TimescaleDB backup.

## What is captured, and where it lands

`backup-root-data.sh` runs a single `rsync -aHAX --delete --inplace $sources "$dest/"`
with `dest=/storage2/backup/minas-tirith`. There is no `--relative`, so **each source
lands under its BASENAME** — which is why `/usr/local` appears as `local/` and not
`usr/local/`:

| Live path | In the backup | Notes |
|---|---|---|
| `/etc` | `etc/` | ⛔ restore NAMED DIRECTORIES ONLY — see below |
| `/home` | `home/` | ⚠️ exclude home-manager dotfiles — see below |
| `/usr/local` | `local/` | restore only while workloads are stopped |
| `/opt` | `opt/` | restore only while workloads are stopped |
| `/srv` | `srv/` | inspect before deciding whether to restore |
| `/var/lib/rancher/k3s/storage` | `storage/` | k3s local-path PVs |
| `/storage/immich-data` | `immich-data/` | `pgdata/` is excluded from backup; restore the DB from its dump |

Bulk media lives on the ZFS pools and is not copied by this job. It has no off-site
copy, so this procedure cannot recover it from a pool failure; confirm the pools and
their datasets are healthy before restoring dependent service state.

## Before you start

Use one root shell so `set -euo pipefail` protects every command and `B` remains set:

```bash
sudo -i
set -euo pipefail
B=/storage2/backup/minas-tirith
```

**1. Stop writers before inspecting or importing storage.** This is an offline
restore. `systemctl stop k3s` is not enough on this host because the unit uses
`KillMode=process`; existing container shims continue running. The packaged killall
script stops k3s, kills the shims, and removes the transient CNI/kubelet mounts
without deleting cluster state.

```bash
systemctl stop backup-root-data.timer backup-root-data.service
systemctl stop zfs-scrub.timer zfs-scrub.service
systemctl stop k3s
k3s-killall.sh

systemctl is-active --quiet k3s && { echo 'k3s still active — STOP'; exit 1; }
pgrep -af 'k3s|containerd-shim' && { echo 'workload process remains — STOP'; exit 1; }
```

The killall script also clears k3s CNI state. That is expected; starting `k3s` after
the restore recreates it.

**2. Import and verify the pools.** Several services bind ZFS paths. Restoring before
the import writes into empty directories at those mountpoints, which then blocks the
ZFS mount.

```bash
zpool status storage storage2
findmnt --mountpoint /storage
findmnt --mountpoint /storage2
```

**3. Confirm the source exists before touching anything.** The live backup tree is a
rolling mirror made with `--delete --inplace`; daily and weekly history is retained as
ZFS snapshots of `storage2/backup`. If the live tree is incomplete, select a known-good
snapshot before continuing.

```bash
[ -d "$B" ] || { echo "BACKUP MISSING — STOP"; exit 1; }
```

## Restore

### Service state outside /etc

```bash
rsync -aHAX --info=stats2 "$B/local/" /usr/local/
rsync -aHAX --info=stats2 "$B/opt/"   /opt/
```

### /home — EXCLUDING home-manager dotfiles

> ⚠️ home-manager owns `~/.config/fish`, `~/.bashrc`, `~/.bash_profile` and
> `~/.config/starship.toml` as **symlinks into the Nix store**. Restoring the old
> regular files over them makes the next activation abort on a file-collision, and
> `nixos-rebuild switch` then fails until repaired by hand.

```bash
rsync -aHAX --info=stats2 \
  --exclude='.config/fish/***' --exclude='.bashrc' --exclude='.bash_profile' \
  --exclude='.profile' --exclude='.config/starship.toml' --exclude='.zshrc' \
  "$B/home/edgar/" /home/edgar/
```

The old fish/tide config remains in the backup if you want to port it into
`users/edgar/home.nix` — restore it **declaratively**, not by copying files back.

### /etc — NAMED DIRECTORIES ONLY

> ⛔ Never `rsync "$B/etc/" /etc/`. NixOS generates `/etc` from the system closure;
> overwriting it wholesale with a captured copy fights activation and can leave the host
> unbootable. Restore only directories with verified current consumers. The current
> hostPath inventory is:

```bash
rsync -aHAX "$B/etc/gameservers/"  /etc/gameservers/    # palworld saves
rsync -aHAX "$B/etc/calibre/"      /etc/calibre/
rsync -aHAX "$B/etc/komga/"        /etc/komga/
rsync -aHAX "$B/etc/lidarr/"       /etc/lidarr/
rsync -aHAX "$B/etc/wrapper/"      /etc/wrapper/
rsync -aHAX "$B/etc/letsencrypt/"  /etc/letsencrypt/
```

`letsencrypt/` holds `acme.json`. See
[ingress-maintenance.md](ingress-maintenance.md) — it must never have two writers, and
every snapshot of it has a shelf life.

### k3s persistent volumes and immich

```bash
rsync -aHAX --info=stats2 "$B/storage/"      /var/lib/rancher/k3s/storage/
rsync -aHAX --info=stats2 "$B/immich-data/"  /storage/immich-data/
```

> ⚠️ `immich-data/pgdata/` and the authentik PVCs are **excluded from the backup on
> purpose** — a live PostgreSQL data directory copied by rsync is not a usable backup.
> Those come back from their dumps in `/storage2/backup/dumps`, per
> [backup-restore.md](backup-restore.md).

### /srv — inspect before restoring

```bash
du -sh "$B/srv"
```

If it contains service data, identify its owner and quiescence requirements before
restoring it. Do not infer safety from an old measured size.

## After

Ownership matters: several services run as uid 911 or 1000. `-aHAX` preserves it; verify
rather than assume:

```bash
ls -ln /usr/local/etc/jellyfin/config/data/data/library.db
```

If database restore is required, keep the relevant database Deployments durably at
zero as described in [postgres-unclean-shutdown.md](postgres-unclean-shutdown.md)
before starting k3s. Then start the agent, perform the database restores from
[backup-restore.md](backup-restore.md), restore desired replicas declaratively, and
verify every workload through its consumer path.

If no database restore is required, start k3s now. In either path, verify workloads
and their consumer-visible state before re-enabling the backup timer:

```bash
systemctl start k3s
systemctl --failed
```

Only after the restore and verification are complete:

```bash
systemctl start backup-root-data.timer
systemctl start zfs-scrub.timer
```

⛔ Do not re-enable `backup-root-data.timer` until the restore is complete and verified.
It runs with `--delete`.
