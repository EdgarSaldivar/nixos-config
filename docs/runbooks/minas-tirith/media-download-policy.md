# Public/private torrent and stalled-download policy

This policy complements `docs/runbooks/minas-tirith/media-hardlinks.md`. It applies to future media downloads;
it does not relink existing files or change tracker obligations retroactively.

## Client boundary

- `deluge-books` is the private-tracker client. MyAnonamouse is books/audiobooks only.
  It keeps `stop_seed_at_ratio=false`, unlimited upload, 80 active seeds, and 80 upload
  slots. Neither Cleanuparr nor the media `*arr` applications may remove its torrents.
- `deluge-vpn` is the public media client used by Sonarr, Radarr, Animearr, and future
  Lidarr. Its port synchronization sidecar also enforces the public policy: stop at ratio
  `2.0`, never remove directly in Deluge, 30 active downloads, 50 active seeds, 80 total
  active torrents, and 40 upload slots.

The public client pauses a torrent at ratio 2. The owning `*arr`, whose Completed Download
Handling and per-client Remove settings are enabled, then removes the torrent and its
download-side link. The library hardlink remains. A manually-added torrent pauses but is
not deleted by Deluge because `remove_seed_at_ratio` stays false.

Do not add MAM or another private tracker to Lidarr, Sonarr, Radarr, or Animearr without
first revisiting this boundary. Lidarr's Prowlarr application sync is disabled while Lidarr
has no root folder or managed artists; its stale MAM indexer was removed. When Lidarr is
enabled, create an explicit music-only Prowlarr routing policy before re-enabling sync.

## Stalled downloads

Cleanuparr is initially report-only. It connects only to Sonarr, Radarr, Animearr, Lidarr,
and `deluge-vpn`; do not configure `deluge-books`, Readarr, Shelfmark, or the books namespace.

Before enabling destructive Queue Cleaner actions:

1. Exclude private torrents by privacy/tracker as a second safety boundary even though the
   configured client is public-only.
2. Apply a 12-hour grace/no-progress window to incomplete public torrents. Use multiple
   strikes, then remove from the public download client, blocklist the exact release in the
   owning `*arr`, and request a replacement search.
3. Never classify an idle completed seed as stalled merely because it has no peers.
4. Keep orphan/download cleaner, seed cleanup, and proactive upgrade search disabled for
   the initial rollout. They are separate deletion/search policies, not prerequisites for
   clearing a stuck queue.
5. Review at least seven days of report-only events, including a deliberately paused public
   canary, before authorizing destructive mode.

For a quality upgrade, the `*arr` imports the better release first. The old library name is
unlinked, but any still-seeding old torrent keeps its inode until the public ratio goal is
met and the owning `*arr` removes it. Never deduplicate different releases by title.

## Acceptance and rollback

After a Deluge rollout, read back the nine enforced core settings through Deluge RPC and
require the exact values above. Confirm the surviving general-client torrents use public
tracker hosts. Confirm MAM is absent from Lidarr and the Prowlarr Lidarr application is
disabled.

Before changing application state, run built-in backups for Prowlarr and all four media
`*arr` applications. To roll back, restore those application backups and revert the Deluge
manifest; do not roll back the media ZFS dataset.
