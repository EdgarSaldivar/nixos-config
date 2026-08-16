# Books hardlink rollout

This is an operator checklist, not an execution record. No production, remote, deploy,
snapshot, or Kubernetes command was run while preparing this change.

## Before deployment

1. Stop and investigate unless `/storage/Media`, `/storage/Media/Torrents`, and
   `/storage/Media/Books` resolve to the intended `storage` ZFS pool and the last two paths
   report the same device number. Confirm UID/GID 1000 can create and remove a test file in
   both directories. Do not substitute a copy test for a hardlink test.
2. Resolve the exact ZFS dataset that backs `/storage/Media` with `findmnt` and `zfs list`;
   take a uniquely named recursive pre-deploy snapshot of that dataset and record the
   snapshot name. Also take the normal application-config backup/snapshot that includes
   Shelfmark's config directory. Verify both snapshots exist before proceeding.
3. Capture the Git revision and SHA-256 hashes of the delivered Shelfmark manifest and
   `/home/edgar/git/docker/books/shelfmark/config/plugins/advanced.json`. Save a copy of
   `advanced.json` with its owner, group, mode, and hash in the evidence directory. Record
   the current Shelfmark image digest and replica count.
4. Review the manifest diff: Shelfmark must have exactly one `/storage/Media` payload mount
   at `/media`; Calibre `/books` and Kavita `/ebooks` must be read-only. Plex paths are out
   of scope and must have no diff.

## Canary and acceptance

1. Deploy only in an approved maintenance window. The init container must log the expected
   `storage` ZFS mount identity, a successful temporary hardlink with `nlink >= 2`, the
   atomic `advanced.json` result, and then exit zero. Any init failure is a hard stop: do
   not bypass it and do not copy the payload manually.
2. Re-hash `advanced.json`, save the new file in evidence, and verify the only intended JSON
   changes are qBittorrent `/downloads` -> `/media/Torrents` and Deluge `/data` ->
   `/media/Torrents`; all other settings and mapping rows must remain present.
3. Import one small, single-file, non-archive book torrent as the canary. Before allowing a
   second import, capture the torrent payload path and resulting `/media/Books` path. For
   both files record `stat -c '%d %i %h %n'` and `sha256sum`. Accept only when device and
   inode match, both link counts are at least 2, and hashes match. Confirm the torrent is
   still seeding and Kavita can read the book through `/ebooks`.
4. Keep the canary evidence with the Git revision, manifest/config hashes, init logs, exact
   paths, device/inode/link-count output, content hashes, and operator timestamp.

## Rollback

Stop new imports first. Revert the declarative manifest change and redeploy through the
normal approved path; do not hand-edit the Deployment. Restore `advanced.json` atomically
from the captured copy (including its ownership and mode) or roll back its application-
config snapshot, then verify its captured SHA-256 hash before starting Shelfmark. If the
canary destination link must be removed, unlink only that verified destination pathname;
confirm the torrent-side inode and hash remain intact. Use the recorded ZFS snapshot only
under the storage restore procedure, because rolling it back discards unrelated later data.

**Never bulk-dedupe, relink, rename, or rewrite Calibre-managed files.** Calibre can mutate
its inputs and metadata; a bulk operation can spread those mutations across seeding inodes
and turn a reversible canary into collection-wide damage.
