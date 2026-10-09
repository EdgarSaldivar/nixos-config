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

The policy prefers HEVC (+1000), HDR (+150), Dolby Vision (+75), and HDR compatible Dolby Vision (+100). Bare Dolby Vision or generic Profile 8 without explicit HDR/HDR10/HDR10+ or DoVi Profile 8.1 gets a -30000 fallback penalty. These are release-title heuristics. H264 and unknown codecs remain eligible with minimum format score 0. No ordinary non-HDR release is blocked. Existing “No bad DV” restrictions remain. For anime, explicit English plus Japanese, Chinese, or Korean audio is preferred (+3000); bare Dual/Multi labels do not establish English. The existing Anime Dual Audio format rewards parsed native language plus a dual-audio hint (+1500), below the explicit English/native bonus but above the codec bonus. Generic Multi-Audio gets no bonus. Confirm the actual languages after import. AV1/AV01/AOM releases receive a -30000 block in every profile, including profiles outside the size-policy targets. Actual AV1 video is also rejected by the optimizer before import. HEVC and H264 remain eligible. Existing source tiers retain their order with a maximum 140; 10bit is 75, Uncensored 100, positive revisions 1–4, and v0 stays negative. The source, bit-depth, and revision bonuses cannot outrank the intended language and codec preference. Audio streams and subtitles, including full dialogue, signs, and fonts, need validation after any later download.

Titles marked Multiple Subtitle, MultiSub, or similar do not earn the explicit English/native audio bonus solely from their language list. Such titles also need a Dual Audio or Multi Audio hint to earn that bonus. This does not establish the actual audio streams; verify them after download. These releases remain eligible without the bonus.

Sonarr and Animearr also prefer season packs (+50) through the native Release Type specification, value 3. The bonus is below the codec and language preferences, and does not apply to Radarr. It favors packs when the other properties are comparable.

The tool merges the existing WEB-DL and WEBRip children with flat BluRay encodes inside each enabled WEB group at 1080p or 2160p, retaining its real group ID and allowed flags. A cutoff that pointed to a moved flat encode points to that enabled group. Remux quality stays available if it was available, but ranks below comparable encodes at the same resolution. Disabled remux and disabled 4K qualities remain disabled; already allowed 1080p fallback in 4K profiles remains. Upgrade is disabled for the initial pilot. CAM, workprint, telecine, and related poor sources are disabled if present.

Preferred sizes are soft MB/min values: Radarr 35/140 at 1080/2160, Sonarr 25/100, Animearr 15/60 at 1080/2160. `maxSize` is unlimited (`null`); existing minimums are preserved, and a soft target below an existing minimum is raised to that minimum. Quality definitions are shared across an app, so changing a definition affects every profile that uses that quality, including profiles outside the explicit targets. Review this impact before apply. Neither file size nor codec establishes visual quality.

## Inventory and later campaign

```sh
python3 scripts/media-library-policy.py inventory --input /path/to/inventory.json --output /secure/candidates.json
```

Input records need exact `app`, `itemId`, `fileId`, `path`, `size` bytes, `resolution`, `device`, `inode`, `title`, and `mediaInfo.runTime` minutes. The calculation uses that file runtime, not title or feature metadata runtime. It deduplicates only matching real device/inode pairs. Ranking estimates **potential logical** savings against the soft target, not physical space reclaimed. It never converts a 4K target to 1080p. GOT/Game of Thrones, LOTR/Lord of the Rings, Hobbit, and Dune are excluded from automatic candidates, including another path to the same inode. Ordinary candidates need at least 30% estimated savings; there is no hard file upper cap.

The campaign policy permits five concurrent optimization downloads and one verification worker, with zero original retention days and a 30-day cooldown. It has no download speed cap, daily quota, or fixed free-space reserve. This bounded configuration tool does not enforce downloads or schedule work.

For a manual pilot, select one exact release and confirm its mapped title, episode, edition, resolution, language hints, seed availability, and expected saving. Exclude whole-season packs from an episode pilot. Before submitting a grab, capture the original's probe metadata and small comparison samples, and extract any English/native subtitles needed by the replacement. Do not create recovery hardlinks or keep old video files after replacement. Record file metadata, chosen release metadata, and the public Deluge client's prior limits in a private runtime ledger under `/home/edgar/.local/state/media-library-optimization/`.

Keep releases with one reported seed eligible, including rare titles. Prefer more seeds among releases with comparable quality, language coverage, edition, and size; a small format-score advantage alone should not outweigh evidence that the alternative can finish. Reported seed counts can be stale, so check connected seeds, distributed availability, and actual progress in Deluge after the grab. Slow progress is not a stall. Follow the grace period and multiple-strike procedure in [media-download-policy.md](media-download-policy.md) before any automated removal or retry; its report-only rollout does not authorize destructive cleanup. These availability checks are part of the manual pilot procedure, not an automated feature of the policy tool.

Keep Deluge's global download limits unchanged. Optimization concurrency belongs to the optimizer, with unlimited speed on its own torrents. Preserve `deluge-books` settings. Keep a failed or uncertain grab recorded and inspect the queue before retrying so a lost response does not create a duplicate.

Validate actual audio and full subtitles (including signs and fonts for anime), runtime, resolution, HDR compatibility, and visual quality before importing a replacement. After import, verify the actual library path and hardlink. If verification fails, reacquire a suitable release rather than restoring a retained original. Remaining torrent links and ZFS snapshots can delay physical space reclamation; distinguish completed logical savings from free space measured on the pool. No unattended campaign is installed by this configuration tool.

## Unattended runner

`hosts/nixos/minas-tirith/media-optimizer.nix` declares the `media-optimizer.service`
and its pinned Python/FFmpeg package. It admits five optimizer jobs across all
three apps, with one verification worker. Downloads and verification awaiting
import share those five slots, so completed staging cannot grow without bound.
There is no speed cap, daily quota, or fixed reserve. Public Deluge's global
limits and other categories are unchanged. The separate `media-optimizer` label
and `/data/optimization/<job>` staging keep normal Arr automatic imports away.
Own torrents disable Deluge completion moves so the same directory remains
available for QA and hardlink import.

```sh
media-optimization status
media-optimization status --json
media-optimization audit-av1
media-optimization concurrency 8
media-optimization pause
media-optimization resume
```

The concurrency override persists in `/var/lib/media-optimizer/journal.sqlite`;
changing the committed campaign value changes the default. `pause` stops new
admissions while existing jobs continue. `systemctl stop media-optimizer`
stops verification and scheduling; already submitted torrents remain in Deluge.
Inspect `journalctl -u media-optimizer` and the atomic status JSON in the state
directory for errors. The status reports logical byte reductions and measured
filesystem free bytes separately, with connected seeds and progress per job.

Largest existing files are considered first. Release ranking retains HEVC and
language tiers, favors comparable packs, and prefers more seeds within a tier.
One reported seed remains eligible. Every selected episode must independently
save at least 30%, so a pack bonus cannot replace an already smaller episode.
AV1 compatibility repairs take priority over ordinary size optimization and can
replace a file with a larger HEVC/H264 download. The 30% saving rule is waived
only for verified AV1 source video. Resolution, compatible video, decodable
main audio, and program-content checks still apply. HDR remains preferred, with
the explicit AV1-only SDR fallback described below; languages are preferences. `audit-av1` scans all Arr-managed files, probes AV1 hints and missing
codec metadata, and records pending repairs plus any probe failures in the state
directory. Protected titles retain their manual-review exclusion for ordinary
size optimization; the explicit AV1 repair request includes them. 4K is never
downscaled.
Artificially interpolated or AI-upscaled releases are excluded.

The runner reads existing Arr XML keys and the public Deluge password in memory;
no credentials or release URLs enter its journal or Nix store. It downloads
torrent metadata first, using a public metadata cache by infohash when needed,
and validates the hash, privacy flag, and file paths before submission. Unknown
magnet metadata is skipped. Every submit/import/remove intent is durable before
the request; restart reconciliation avoids repeating an uncertain mutation.
Temporary Arr transport failures and transient HTTP errors retain the payload and
the durable import intent. API retries back off from 15 seconds to five minutes;
`api_error` and `api_retry_at` appear in job status. A lost import POST response is
reconciled through Arr's consumer file before any further action. API outages do
not consume the media-rejection retry allowance or blacklist the release.

Explicit diagnostic rechecks in the journal's `manual_rechecks` setting take the
next available pipeline slot and require the requested torrent hash. They do not
increase concurrency or bypass QA. A rejected diagnostic candidate is paused and
kept in staging for inspection; ordinary rejection cleanup remains automatic.
These are incoming candidates, not retained original library videos.

Season and multi-season packs count as one pipeline slot. Torrent filenames
with unambiguous season/episode numbering expand the targets to every covered
eligible library file, including ordinary files that independently meet the
savings gate. Deluge file priorities select those videos, matching subtitle
sidecars, and fonts; other episodes and samples are skipped. The payload savings
gate uses the selected bytes. A pack may supply one episode, a whole season, or
several seasons. An ambiguous absolute-numbered pack can retain its full payload
and use exact consumer episode mappings as a fallback. Contradictory explicit
season numbering is rejected. Each selected file still requires independent QA.

Pre-import checks inspect streams and HDR compatibility, decode representative
samples, compare sampled program content, and preserve useful
English/native source subtitles as small sidecars where possible.
Dolby Vision claiming an HDR10-compatible base layer must not contradict that
claim with explicit transfer, primaries, or matrix tags. When preserving source
HDR, missing tags need decoded-frame evidence. This checks the incoming
release's own signaling.
Native audio, English dubs, surround, Atmos, and subtitles are preferences, not import gates.
Mono/stereo, native-only audio, or missing subtitles remain eligible. Missing
audio languages, reduced channel counts, lost Atmos, and subtitle failures are
recorded in QA metadata and status. At least one decodable main audio track is
required. Source soundtracks provide optional local scene-search hints, never
a verdict about the replacement's audio/video synchronization. Weak audio
samples can be retried 15 seconds either side; missing correlation, different
dubs, or changing offsets do not reject the video.
A sole untagged main audio track can supply an optional scene-search hint. This
does not assign it a language or make soundtrack correspondence an import gate.
Four scene checks span 15%, 40%, 65%, and 85% of the program. Credits are not an
identity gate. Each scene is matched at its own position in the replacement.
A two-frame seek tolerance, local picture searches, and nearby reference retries
handle cuts, seek rounding, and uninformative source samples.
Picture matching also compares smoothed luminance ranks and edges under small
spatial shifts and up to 4% scale adjustments, accommodating framing and grading
differences without lowering the content-match threshold.
Frame cadence is
measured from packet presentation timestamps for evidence; it is not a requirement
to reproduce the original frame rate or a basis for subtitle speed changes.
For nearly identical picture matches, the nearest expected timestamp wins, so
held animation frames do not invent offsets. Strong, ordered picture matches
must support one linear timeline within 150 ms to transfer original subtitles.
Otherwise original subtitles are not installed; embedded replacement subtitles
and background fetching/synchronization supply them. That is a subtitle repair,
not a video rejection. Matching program content remains required; scene order
and runtime differences are correspondence evidence, not a source timing standard.
Runtime differences, measured frame rates, and the previous matched scene can
suggest additional search positions, including a removed or added opening;
actual pictures must match independently at all four checkpoints.
Explicit movie edition requirements remain part of release selection.
Frame and audio correspondence evidence is saved for diagnosis.
Each scene's `checkpoint-N/attempts.json` records its decision and search attempts;
small original/replacement grayscale samples and local-search scores are saved
for rejected checkpoints as well as accepted ones.
The FFmpeg input
timestamp options are documented at <https://ffmpeg.org/ffmpeg.html>.
Bundled sample clips, trailers, and files in sample/extra directories are excluded
before mapping and QA, even if Arr assigns them to the same movie. A sole incoming
movie feature with an unparsed filename is reprocessed by Radarr using the movie
ID already established by its release and a parseable incoming hardlink alias.
This alias shares the downloaded bytes and leaves Deluge's filename intact; it
is removed after a verified import or rejection. Ambiguous multi-feature payloads or a
contradictory parsed movie ID are never forced. Reprocessing does not import;
stream, content, and hardlink verification still precede replacement.
This verifies picture correspondence and decodability, not subjective dub quality.
ASS styling, signs, and embedded font assets are retained where possible. Missing
English/native subtitles remain eligible for background fetching. Among candidates with equal
codec/language tier, availability, pack status, seed count, and format score,
advertised 7.1 is preferred if its size is within 20% of the smallest comparable
release. Premium titles remain excluded from ordinary unattended optimization.
Picture correlation checks
content correspondence; it is not a universal perceptual quality guarantee.
Ambiguous program content/editions, unsupported video, or resolution/HDR losses
isolate a movie or the affected episode. A pack's failed episode releases its
reservation for an alternative; other files continue QA and import. Missing
episode mappings in a completed pack are rejected individually. A bad episode
gets one alternative before a one-day cooldown. An episode that Arr later
replaces through another release no longer counts as an active
rejection; its original reason is kept in journal history. A missing original
alone does not establish a successful replacement. Terminal movie/episode
failures are also reconciled every five minutes against the current Arr consumer
ID and actual file identity. Superseded failures retain their original reason
in history and are counted separately from unresolved reviews.
If at least eight files in a season have been checked and at least four (30% or
more) were rejected, status reports a pack-quality warning to inspect shared
metadata or timing issues. This warning does not override individual QA results.
No original video is copied or retained. Imports use Arr's hardlink path, then
verify the actual library inode, size, and probe; subtitle installation is best effort. Own public
torrents seed to ratio 2; cleanup verifies the library hardlink before deleting
the staging payload. Normal torrents are never removed by this runner.

Slow byte progress remains healthy. After the configured 30 minutes without
new bytes and a second observation at least five minutes later, the runner
searches for other work even when all five slots are occupied. It removes only
its own stalled partial download, and only after another eligible public
torrent's metadata and size have been validated. With no suitable alternative,
the current download keeps waiting. Completed files already in QA/import are
not preempted. A yielded release and its torrent hash get a six-hour availability
backoff; its title gets one hour, without consuming the QA retry allowance.
Movie QA failures are remembered by torrent hash. Pack QA failures are remembered
by hash and episode ID, so another indexer cannot retry the same rejected file
while healthy episodes in that pack remain eligible.
For an existing HDR file, explicit HDR/DV release hints rank ahead of an
unlabelled codec bonus; unlabelled releases remain a fallback and actual stream
inspection preserves HDR for ordinary optimization. For an actual AV1 source,
available advertised HDR candidates are tried first; once those are exhausted,
a non-HDR candidate can repair playback using SDR with an explicit QA tradeoff.
The waiver never applies to a non-AV1 source, resolution loss, unsupported video,
or conflicting Dolby Vision signaling. Missing color tags trigger two decoded
frame samples; unestablished HDR is reported as uncertain rather than confirmed
SDR. Ten-bit video alone does not establish HDR.
Progress resets the count; queued, paused, checking, seeding, and API outages
do not count as stalls.
An actual ENOSPC error temporarily stops admissions. The known degraded pool
state does not independently halt the campaign or re-enable disk notifications.

For a targeted installation without switching other Minas services, copy the
tree to an absolute host path, then build the generated unit bundle on Minas:

```sh
sudo nix build --no-update-lock-file --no-write-lock-file \
  '/absolute/source#nixosConfigurations.minas-tirith.config.system.build.mediaOptimizationUnits' \
  --out-link /nix/var/nix/gcroots/media-optimizer
```

Run the generated unit's `ExecStart` command with `preflight` in place of `run`,
as `edgar`, after creating `/var/lib/media-optimizer` owned by `edgar:users` with
mode 0700. Install the bundle's services and timers under `/usr/local/lib/systemd/system/`.
Link the optimizer, subtitle bridge, and both Bazarr services from
`multi-user.target.wants`, and link both setup timers from `timers.target.wants`. Install the
`MEDIA_OPTIMIZER_CONTROL` wrapper as `/usr/local/bin/media-optimization`.
For this targeted installation, use `/usr/local/bin/media-optimization` explicitly
if `/usr/local/bin` is absent from the shell PATH. Verify with
`systemd-analyze verify`, reload systemd, and start only these media units.
Keep the GC root. A later full NixOS activation installs the declared unit and
control command normally; remove the administrator symlinks and temporary GC
root only after verifying those declared paths are active.

The subtitle services use pinned Bazarr from nixpkgs: `main` connects to Radarr
and Sonarr, while `anime` connects to Animearr. They listen only on localhost
ports 16767 and 16768. The read-only Arr bridge on localhost 18787 loads existing
keys in memory and forwards approved metadata GET endpoints. Bazarr's on-disk
configuration contains a non-secret loopback placeholder, never an Arr key.
SignalR is disabled; library polling runs every 15 minutes, missing-subtitle
searches every six hours, and one worker per instance handles provider searches
and synchronization. YIFY Subtitles, TVsubtitles, and AnimeTosho are enabled without
account credentials. No paid provider, translation, or transcription is enabled.
Provider availability and matching subtitles are not guaranteed.

Setup timers reconcile English plus the title's Japanese, Korean, or Chinese
original language hourly, assigning English-only to other original languages.
Existing non-MLP subtitle profiles are preserved. Full-dialogue profiles have no
cutoff and preserve original subtitle format. Embedded subtitles count toward
the desired languages, and downloaded subtitles use Bazarr's synchronization.
Inspect each localhost UI with an SSH tunnel when provider setup needs adjustment.

Release-title hints for explicit English subtitles score +1500, identified
Japanese/Korean/Chinese subtitles score +500 in Animearr, parsed English audio
scores +350, surround +50, and Atmos +100. Generic MultiSub labels alone receive
no subtitle bonus. These hints do not establish actual streams or subtitle timing.
Radarr target profiles use language Any, with English expressed as a positive
custom-format preference; their earlier English-only language gate is removed.

The media manifests set memory limits of 4 GiB for Sonarr and 2 GiB each for
Radarr and Animearr. These are ceilings; requests remain 256 MiB. Sonarr needs
headroom to deserialize its cached media metadata. Apply changes through
Pelargir under the existing `minas-sonarr.yaml`, `minas-radarr.yaml`, and
`minas-animearr.yaml` AddOn basenames, then verify each media rollout and API.
