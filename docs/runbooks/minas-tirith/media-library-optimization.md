# Media library optimization policy

This is an offline review procedure for the bounded `scripts/media-library-policy.py` tool. The committed policy is `policies/media-library-policy.json`. This tool edits only *arr custom formats, quality profiles, quality definitions, and exact x264/H264 ignored terms in release profiles. A codec-only release profile is deleted when removing those terms would leave an invalid empty rule. It does not search, download, assign titles, change monitoring, delete media, schedule work, or measure stream quality.

## Prepare and review

Supply API base URLs and keys directly to the caller through `MEDIA_POLICY_RADARR_URL`, `MEDIA_POLICY_RADARR_API_KEY`, and the equivalent `SONARR` and `ANIMEARR` names. The command accepts `--url app=URL` and `--key app=KEY` too. Use base URLs without query strings or embedded credentials. The tool reads `/api/v3` collections. It does not read credential files or SQLite.

```sh
python3 scripts/media-library-policy.py plan --output /secure/reviewed-media-plan.json
```

For offline tests, `plan --fixture /path/to/sanitized-snapshot.json --output /secure/reviewed-media-plan.json` reads a JSON object keyed by `radarr`, `sonarr`, and `animearr`, with each app containing `qualityprofile`, `qualitydefinition`, `customformat`, and `releaseprofile` arrays. Review every operation and its `before` and `desired` values. The `reviewHash` covers the policy hash, complete source state hash, and operations. A profile references new custom formats by symbolic name until the API returns their real IDs.

Targets are explicitly Radarr profiles `1:1080`, `5:2160`, `7:2160`; Sonarr `1:1080`, `4:1080`, `5:2160`, `7:2160`; and Animearr `7:1080`, `5:2160`. IDs and profile names remain in place because Seerr uses Radarr 1, Sonarr 1, and Animearr 7. A target declared 1080 is refused if it has an enabled 4K quality. The tool leaves title assignments and monitoring alone.

## Apply after review

```sh
python3 scripts/media-library-policy.py apply --reviewed /secure/reviewed-media-plan.json --review-hash REVIEW_HASH_FROM_PLAN --progress /secure/media-progress.json
```

Apply rereads all targeted collections, checks for drift, and recomputes the entire plan from the current policy. It will not replay edited operations. When there are changes, it starts and waits for the built-in Backup command on **every targeted app** before making any configuration write. A failed or pending backup at timeout prevents all configuration writes. Configuration is read again after backups; drift also prevents writes. The progress file is atomically written with mode `0600` before the first write and after each write and readback. It contains the original collection snapshots, backup command IDs, completed writes and returned custom format IDs, pending operations, and an `inFlight` entry for a request whose response may be lost. Readback mismatch or a partial failure exits nonzero. Keep this file with the app backups for manual recovery; inspect the recorded `before` objects, completed IDs, and any `inFlight` entry before restoring. An immediate rerun of the same reviewed plan will refuse because state has changed; make a fresh plan after recovery or after a successful apply. A successful replan should contain zero operations.

## Configured preferences and review limits

The policy prefers HEVC (+1000), HDR (+150), Dolby Vision (+75), and HDR compatible Dolby Vision (+100). Bare Dolby Vision or generic Profile 8 without explicit HDR/HDR10/HDR10+ or DoVi Profile 8.1 gets a -30000 fallback penalty. These are release-title heuristics. H264 and unknown codecs remain eligible with minimum format score 0. No ordinary non-HDR release is blocked. Existing “No bad DV” restrictions remain. For anime, explicit English plus Japanese, Chinese, or Korean audio is preferred (+3000); bare Dual/Multi labels do not establish English. The existing Anime Dual Audio format rewards parsed native language plus a dual-audio hint (+1500), below the explicit English/native bonus but above the codec bonus. Generic Multi-Audio gets no bonus. Confirm the actual languages after import. Existing source tiers retain their order with a maximum 140; 10bit is 75, Uncensored 100, positive revisions 1–4, and v0 stays negative. The source, bit-depth, and revision bonuses cannot outrank the intended language and codec preference. Audio streams and subtitles, including full dialogue, signs, and fonts, need validation after any later download.

Titles marked Multiple Subtitle, MultiSub, or similar do not earn the explicit English/native audio bonus solely from their language list. Such titles also need a Dual Audio or Multi Audio hint to earn that bonus. This does not establish the actual audio streams; verify them after download. These releases remain eligible without the bonus.

The tool merges the existing WEB-DL and WEBRip children with flat BluRay encodes inside each enabled WEB group at 1080p or 2160p, retaining its real group ID and allowed flags. A cutoff that pointed to a moved flat encode points to that enabled group. Remux quality stays available if it was available, but ranks below comparable encodes at the same resolution. Disabled remux and disabled 4K qualities remain disabled; already allowed 1080p fallback in 4K profiles remains. Upgrade is disabled for the initial pilot. CAM, workprint, telecine, and related poor sources are disabled if present.

Preferred sizes are soft MB/min values: Radarr 35/140 at 1080/2160, Sonarr 25/100, Animearr 15/60 at 1080/2160. `maxSize` is unlimited (`null`); existing minimums are preserved, and a soft target below an existing minimum is raised to that minimum. Quality definitions are shared across an app, so changing a definition affects every profile that uses that quality, including profiles outside the explicit targets. Review this impact before apply. Neither file size nor codec establishes visual quality.

## Inventory and later campaign

```sh
python3 scripts/media-library-policy.py inventory --input /path/to/inventory.json --output /secure/candidates.json
```

Input records need exact `app`, `itemId`, `fileId`, `path`, `size` bytes, `resolution`, `device`, `inode`, `title`, and `mediaInfo.runTime` minutes. The calculation uses that file runtime, not title or feature metadata runtime. It deduplicates only matching real device/inode pairs. Ranking estimates **potential logical** savings against the soft target, not physical space reclaimed. It never converts a 4K target to 1080p. GOT/Game of Thrones, LOTR/Lord of the Rings, Hobbit, and Dune are excluded from automatic candidates, including another path to the same inode. Ordinary candidates need at least 30% estimated savings; there is no hard file upper cap.

The campaign policy limits replacements to one concurrent download, 50 GiB per day, 5 MiB/s, a 4 TiB free-space floor, zero original retention days, and a 30-day cooldown. This bounded configuration tool does not enforce downloads, schedules, or those runtime limits.

For a manual pilot, select one exact release and confirm its mapped title, episode, edition, resolution, language hints, seed availability, and expected saving. Exclude whole-season packs from an episode pilot. Before submitting a grab, capture the original's probe metadata and small comparison samples, and extract any English/native subtitles needed by the replacement. Do not create recovery hardlinks or keep old video files after replacement. Record file metadata, chosen release metadata, and the public Deluge client's prior limits in a private runtime ledger under `/home/edgar/.local/state/media-library-optimization/`.

Keep releases with one reported seed eligible, including rare titles. Prefer more seeds among releases with comparable quality, language coverage, edition, and size; a small format-score advantage alone should not outweigh evidence that the alternative can finish. Reported seed counts can be stale, so check connected seeds, distributed availability, and actual progress in Deluge after the grab. Slow progress is not a stall. Follow the grace period and multiple-strike procedure in [media-download-policy.md](media-download-policy.md) before any automated removal or retry; its report-only rollout does not authorize destructive cleanup. These availability checks are part of the manual pilot procedure, not an automated feature of the policy tool.

During the pilot, set only the public media Deluge client to `max_active_downloading=1` and `max_download_speed=5120` KiB/s. Set the selected torrent's `max_download_speed` to the same value. Preserve `deluge-books` settings. Check the 4 TiB reserve after allowing for the download, and count each selected torrent's full size against the daily budget. Keep a failed or uncertain grab recorded and inspect the queue before retrying so a lost response does not create a duplicate.

After import, validate actual audio and full subtitles (including signs and fonts for anime), runtime, resolution, HDR compatibility, and visual quality against the captured evidence before advancing to the next movie, TV episode, or anime episode. Remove any previously created recovery copy immediately after confirming that its verified replacement remains present. If verification fails, reacquire a suitable release rather than restoring a retained original. Restore Deluge's recorded prior limits after the pilot. Remaining torrent links and ZFS snapshots can delay physical space reclamation; distinguish completed logical savings from free space measured on the pool. No unattended campaign is installed by this tool.
