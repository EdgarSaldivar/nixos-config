# Media hardlink rollout

This runbook covers Deluge imports into Sonarr, Radarr, Animearr, and Lidarr. It does not
relink existing library files or change the books download stack.

## Invariants

1. `/storage/Media`, `/storage/Media/Torrents`, and every destination root must resolve to
   the `storage` ZFS filesystem and report the same device number on minas-tirith.
2. Each `*arr` Pod gets exactly one payload bind mount:
   `/storage/Media` on the host at `/storage/Media` in the container. Do not add a second
   `/data`, `/downloads`, `/movies`, `/tv`, or `/music` mount. Separate bind mounts return
   `EXDEV` for `link(2)` even when their host paths are on the same filesystem.
3. In all four applications, **Use Hardlinks instead of Copy** stays enabled. Deluge's
   completed path is `/data/completed`, so each application needs this Remote Path Mapping:

   | Host | Remote Path | Local Path |
   | --- | --- | --- |
   | `deluge-vpn` | `/data/` | `/storage/Media/Torrents/` |

   The mapping is a prefix replacement. It makes Deluge's reported
   `/data/completed/<release>` resolve inside the single payload mount as
   `/storage/Media/Torrents/completed/<release>`.
4. Destination roots stay separate from the download directory. Current roots are
   Sonarr `/storage/Media/Television` and `/storage/Media/Anime`, Radarr
   `/storage/Media/Movies` and `/storage/Media/Anime`, and Animearr
   `/storage/Media/Anime`. Lidarr currently has no root or managed artists; use
   `/storage/Media/Music` when it is enabled.

## Rollout

1. Confirm every `*arr` Activity queue is empty. Capture the four media-management,
   download-client, and remote-path-mapping API resources as rollback evidence without
   printing API keys or download-client passwords.
2. Add the mapping above to Sonarr, Radarr, and Animearr before the Deployment rollout.
   Their old Pods already have the full media mount, so the mapped local path is valid.
3. Deliver the four manifests through the normal pelargir auto-manifest path and wait for
   each `Recreate` Deployment to become Ready. Do not hand-edit the live Deployment.
4. Add the same mapping to Lidarr after its new Pod is Ready. Do not create a Lidarr root
   unless music management is intentionally being enabled.
5. In each Pod, create a temporary file below
   `/storage/Media/Torrents/completed`, hardlink it into that application's destination
   root, and immediately unlink both names. Accept only if `stat -c '%d %i %h %n'` reports
   matching device and inode values and link counts of at least 2.
6. Import one small release as a canary. Record the torrent payload and renamed library
   paths. Accept only if both names have the same device and inode, both link counts are at
   least 2, the torrent is still seedable, and Plex can read the library path. A matching
   checksum alone is not proof of a hardlink.

## Multiple releases for one title

Different releases of the same title are different files and must never be hardlinked to
each other merely because their titles match.

- A quality upgrade is normal: the `*arr` application imports the better release as a new
  hardlink and unlinks the superseded library name. If the old torrent is still present,
  its download-side name keeps the old inode and remains seedable. The old inode consumes
  space until that torrent is removed.
- Completed Download Handling and per-client **Remove** stay enabled. For torrents, removal
  occurs only after Deluge reports the torrent stopped at its seed goal. A post-import
  category must not be set because it prevents this cleanup.
- Prowlarr owns the mixed-client seed policy: every public torrent indexer has Seed Ratio
  `2.0`; private torrent indexers have no seed ratio. Deluge's global ratio stop must stay
  disabled. Public torrents therefore stop at `2.0` and the owning `*arr` removes them
  after import, while private torrents continue seeding and are never automatically
  removed. Do not enable a client-wide cleanup rule against `deluge-vpn` unless it can
  positively select public torrents and exclude private trackers.
- A stalled torrent is not covered by Servarr failed-download handling. Until an approved
  cleanup controller is deployed, remove it from the application's Activity queue with
  **Remove from download client** and **Blocklist release** selected; then search again.
- Never delete or relink files by matching title text. Use the `*arr` queue/history download
  ID and Deluge infohash to identify the exact release.

## Rollback

Stop new grabs first. Restore the captured remote-path-mapping resources, revert the four
manifests, and redeploy through pelargir. Existing hardlinked imports remain valid: removing
either directory entry does not damage the other. Do not roll back the whole ZFS dataset
for an application configuration error.
