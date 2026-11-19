{
  # Enabled: the migration/bucket-init Job runs and the API and model Deployments
  # each carry one replica. The legacy Compose state was restored beforehand under
  # separate authority: the PostgreSQL dump reproduced all 52 tables with an
  # identical row inventory at Alembic head 20260720_0031, and legacy MinIO held a
  # single bucket with zero objects, so the Job's bootstrap creates it. The stopped
  # Compose containers and their volumes are retained, not deleted.
  #
  # Sheets' reader on edoras (the owner's "use edoras for now" and, for its address, "sounds okay", 2026-11-18):
  # config only, no new images; the pins below stay b963c92e's. nardol's GPU now runs another project, so sheets fell
  # back to Tesseract. The manifest points both ConfigMaps (Read again in the API, the nightly import job) at edoras's
  # Qwen3.5-9B at its Tailscale address, http://100.80.226.32:8000/v1, model "default", no key; edoras's
  # `tailscale serve --tcp=8000` forwards it to vLLM's loopback port, so edoras switches first. The address rather
  # than the MagicDNS name keeps the tailnet's name out of this public repository. Temporary: edoras isn't at its
  # permanent location yet, and nardol at http://100.66.174.88:8000/v1 is the fallback. The API restarts onto it
  # through its release-reader annotation, and the migration Job reruns because the API ConfigMap is in its name
  # (idempotent; head stays 20261116_dbidx01).
  #
  # Release b963c92e (the owner's go for this server release, 2026-11-18): PinCollector #396, #399 and #400, 23
  # commits on top of a2fc18d2 (the rest of the range is iOS only, #401, and docs, #395, #397 and #398, none of them
  # under backend/). Suggest details keeps eBay results like any other site, and Google's /goto links and eBay's
  # ebay.us shortener are dropped as links we can't check, like the other redirects and shorteners (#396). The
  # release-sheet and news feeds go out as a generic client, httpx's own default user agent, not a named one (#399,
  # ADR 0021). Each sheet row gets its own art: one picture per row, chosen for the whole panel at once by distance from
  # the row's item-number or edition line, and a row holding art that a row left without could have had is flagged
  # for a check (#400, app/catalog/release_sheet_placement.py). Backend code only: no migration (head stays
  # 20261116_dbidx01), no new settings, and #400's flag is a new message under the existing "art" key, so an
  # image-only rollback to a2fc18d2 is safe. The model image is the live base relabelled (model-service/, seed-data/
  # and .dockerignore unchanged since a2fc18d2; RootFS matches). Published by hand on nardol; both revision labels and
  # the API's .build-sha verified.
  #
  # Release a2fc18d2 (the owner's go for this server release, 2026-11-17): PinCollector #387 and #390, 32 commits on
  # top of 442db6b7 (the rest of the range is iOS only: #388, #389, #391, #392 and #393, none under backend/).
  # POST /diagnostics/app's own body cap goes from 64 to 128 KiB (#387); the edge adds no lower limit, since the
  # pin-collector route has no buffering middleware (traefik-routes/catalog.nix). App diagnostics keep and show what
  # the phone was doing during a Neural Engine load (CPU, memory, camera, cores, Low Power Mode) and the build's steps
  # after it (#390), in the report JSON; the admin Phones page shows them. Backend code only: no migration (head stays
  # 20261116_dbidx01), no new settings, so an image-only rollback to 442db6b7 is safe (its report models ignore the new
  # keys). The model image is the live base relabelled (model-service/, seed-data/ and .dockerignore unchanged since
  # 442db6b7; RootFS matches). Published by hand on nardol; both revision labels and the API's .build-sha verified.
  #
  # Release 442db6b7 (the owner's go for the server PRs, 2026-11-17): PinCollector #377, #378, #376, #371 and #373, 99
  # commits on top of 2748c28 (the rest of the range is iOS, model tooling and docs: #350, #352, #356, #370, #379-#386,
  # none under backend/). The server's heuristic and rembg pin cutouts retired (#377; seven pin_cutout_* tuning
  # settings go, none set here; PIN_COLLECTOR_PIN_CUTOUT_BACKEND stays, disabled here); the Releases workbench reads
  # the catalog once per page (#378); the import's look-alike words come from a letter-pair index (#376); My Pins
  # finds a response's image strings by a per-class plan (#371); check-pack answers If-None-Match from row versions,
  # its look-alikes kept current off the request path (#373). Backend code only: no migration (head stays
  # 20261116_dbidx01), no new settings, so an image-only rollback to 2748c28 is safe. The model image is the live base
  # relabelled (model-service/, seed-data/ and .dockerignore unchanged since 2748c28; RootFS matches). Published by
  # hand on nardol; both revision labels and the API's .build-sha verified.
  #
  # Release 2748c28 (the owner's "bundle it then", 2026-11-16): PinCollector #353-#369 and #372-#375, 135 commits on
  # top of 97b3ded. DINOv3 8-bit's server half (#353): a second on-device contract, dinov3b-norm-v3, beside v2 (v2
  # untouched for builds <= 8), with sibling copies so each fleet sees the other's new pins, an off switch
  # (PIN_COLLECTOR_DISABLED_EMBEDDING_CONTRACTS, unset here) and the refingerprint tooling. Two additive migrations,
  # chained, each under lock_timeout 5s: 20261116_embcopy01 (pin_embeddings.copied_from, nullable FK) and
  # 20261116_dbidx01 (five lookup indexes from the query audit). The query audit's fixes (#361 Catalog Reports by id,
  # #363 batched catalog reads, #368 admin posts off the event loop, #369 grouped batch writes, #364 the re-enable
  # 500), single-threaded BLAS in the API image (#375), app diagnostics' new scan and model-load fields (#354, #355).
  # Production-like settings now require PIN_COLLECTOR_STORAGE_BACKEND=postgres, which every manifest here sets. No
  # new settings. The model image is the live base relabelled (model-service/, seed-data/ and .dockerignore unchanged
  # since 97b3ded; RootFS matches). Published by hand on nardol; both revision labels and the API's .build-sha
  # verified.
  #
  # Release 97b3ded (the owner's "do your recommendations", 2026-11-15): PinCollector #338 on top of d818ec8. The news
  # import joins the same story from two sites whatever kind each was given (the trial kept WDWNT's and BlogMickey's
  # passholder-merchandise story twice: one read as a release, one as an event). Backend code only: no migration, no new
  # settings. The model image is the live base relabelled (RootFS matches). Published by hand on nardol; both revision
  # labels and the API's .build-sha verified.
  #
  # Release d818ec8 (the owner's "go for it" to this release, 2026-11-15): PinCollector PRs 321-337. The news import
  # (#327-#330): migration 20261112_news01 (additive: feed fields on home_news, nullable or defaulted; the
  # news_sources, news_dropped_items and news_import_runs tables; the import's master switch, off), the admin News
  # page with each source's robots and terms check, the run and the trial harness. newsImport is set (the owner's
  # choice), so the news-import CronJob (#172) runs every 3 hours and reads nothing while the master switch is off
  # and no source is on. Home's cards (#326, #329, #331, #336) read existing routes. The rest is
  # app-only (#334 warm-up marks, #337 board list) or tests and debug tooling (#321, #335). No new settings. The model
  # image is the live base relabelled (model-service/, seed-data/ and .dockerignore unchanged since caf6fa0; RootFS
  # matches). Published by hand on nardol; both revision labels and the API's .build-sha verified.
  #
  # Release caf6fa0 (the owner's approval of the build-5 critical path, 2026-11-13): PinCollector PRs 312-333 that
  # touch the server. Crash diagnostics (#324): app diagnostics accept MetricKit crash/hang/CPU/disk entries, exit
  # counts by reason and a killed-scan summary, kept in the report JSON (no migration); POST /diagnostics/app gets its
  # own 64 KiB body cap; the admin Phones page shows Crash / Killed scan lines; the privacy page's app-diagnostics
  # sentence names them. The board-scan start marker (#332): one additive migration, 20261112_scanphase01, widens
  # ck_board_scan_exits_phase for the new phases start and waiting_for_scanner (runs under lock_timeout 5s); build 5
  # needs it before it ships, since the old enum answers 422. Home's newest pins (#325: /catalog/pins?cutout=true,
  # catalog_new on /releases), the app-storage breakdown in diagnostics (#319), approving a release row links both
  # resorts' pins (#320). No new settings. The model image is the live base relabelled (model-service/, seed-data/ and
  # .dockerignore unchanged since a13a993; RootFS matches). Published by hand on nardol; both revision labels verified.
  #
  # Release a13a993 (the owner's "deploy all u got my okay", 2026-11-12): PinCollector PRs 299-315. In-app feedback
  # (POST /feedback, signed in or out; an admin Feedback inbox under Community; answers emailed through Resend as
  # support@, no notification email); park sightings only at shops that sell pins (brand stores and food stands never
  # report, World of Disney at Disney Springs and EPCOT's pin kiosks added, shop names fixed), the store menu's private
  # correction labels, and "Still here?" / "Gone" answers; the public pages' favicon and apple-touch-icon (the
  # Starlight icon). Three additive migrations, chained: 20261112_feedback01 (feedback_messages, feedback_replies),
  # 20261112_stillhere01 (sighting_answers) and 20261112_sightlabel01 (sighting_labels); an older image's migration Job
  # wouldn't know the new head, so a rollback is a fix forward or an explicit downgrade. No new settings (the two new
  # ones default). The privacy page gains the feedback, Still here and correction-label sentences. The model image is
  # the live base relabelled (model-service/, seed-data/ and .dockerignore unchanged since 8adcac7; RootFS matches).
  # Published by hand on nardol; both revision labels and the API's baked build fingerprint were verified before rollout.
  #
  # Release 8adcac7 (the owner's "deploy backend", 2026-11-12): PinCollector PRs 286, 287, 290, 292 and 293. Monthly
  # sheets read with prompt v3 ("N Pins On A Card" stays in names; printed series positions always filled; tuned on
  # nardol's Qwen3.5-9B). A public landing page at / with a Smart App Banner that stays off until an App Store id is set
  # (none yet, so it says "coming soon"), and versioned static files served immutable. Photo screening: catalog photos
  # checked with OpenAI moderation (off until photoChecks after the dry run), People reports flags, reviewers can turn
  # accounts off. The admin and server say PinStellation. One additive migration, 20261112_screen01 (catalog_media
  # check columns, a check constraint, a partial index, account disabling); an older image's migration Job wouldn't
  # know the new head, so a rollback is a fix forward or an explicit downgrade. The model image is the live base
  # relabelled (model-service/, seed-data/ and .dockerignore unchanged since 8c61999; RootFS matches). Published by
  # hand on nardol; both revision labels and the API's baked build fingerprint were verified before rollout.
  #
  # Release 8c61999 (the owner's "ok for ghcr" and "you can merge anything you got", 2026-11-11): PinCollector PRs
  # 280-284. The app becomes PinStellation: bundle id app.pinstellation (Sign in with Apple audience and APNs topic
  # defaults follow, so builds still carrying io.saldivar.pincollector or app.pincollector lose Sign in with Apple), and
  # every collector-visible "PinCollector" in public pages, legal pages and emails reads PinStellation. Price history:
  # an append-only price log fed by release-sheet retail prices, set and mystery-box prices kept whole, "Sold new" per
  # series, wave and pin (Hidden Disney seeded as never sold), and an admin's original price recorded too. Two additive
  # migrations: 20261111_price01 (pin_price_observations + flags) and 20261111_price02 (set members, catalog_set_sale
  # with its Hidden Disney seed); an older image's migration Job wouldn't know the new head, so a rollback is a fix
  # forward or an explicit downgrade. The manifest also gains PIN_COLLECTOR_SUPPORT_EMAIL. The model image is the live
  # base relabelled (model-service/, seed-data/ and .dockerignore unchanged since ee447d2; RootFS matches). Published
  # by hand on nardol; both revision labels and the API's baked build fingerprint were verified before rollout.
  #
  # Release ee447d2 (the owner's "start immediate testing and deployment" for SearchApi, 2026-11-11): PinCollector PRs
  # 278 and 279. Suggest details' outside lookups give the provider a short-lived unlisted link to the cutout (90 s by
  # default, at most 300) instead of uploading it; SearchApi joins SerpApi as a provider, with the admin choosing the
  # order and a fallback; each lookup records its search and reader seconds and which provider answered. The privacy
  # page says so (owner-approved wording). Two additive migrations: 20261110_lookup01 (detail_lookups timing columns,
  # detail_lookup_pictures) and 20261110_lookup02 (provider CHECKs widened to searchapi, fallback_provider,
  # searchapi_searches). An older image's migration Job wouldn't know the new head, so a rollback is a fix forward or an
  # explicit downgrade. SearchApi stays unusable until its key is mounted (a separate nixos-config change). The model
  # image is the live base relabelled (RootFS matches). Published by hand on nardol; both revision labels and the API's
  # baked build fingerprint were verified before rollout.
  #
  # Release b3662f2 (the owner's "yeah" to building and shipping sheet reader v2, 2026-11-10): PinCollector PR 277. Monthly
  # sheets: each panel is sent alone (other panels' headings blanked) with its own heading's date, a new prompt, whole-dollar
  # prices, UPC check digits on item numbers, the passholder or Magic Key mark found on the image, one bad panel no
  # longer drops the vision reader, and "Read again" no longer duplicates or merges rows; plus a bigger-art viewer on the
  # release workbench. Scored 693/693 on four November sheets (681 before). No migrations and no schema change, so an
  # image-only rollback to 2185cdd is safe. The model image is the live base relabelled (model-service/ and seed-data/
  # unchanged; RootFS matches). Published by hand on nardol; both revision labels and the API's baked build fingerprint
  # were verified before rollout.
  #
  # Release 2185cdd (the owner's "add nardol as an endpoint ... then run it" and "do everything you need",
  # 2026-11-10): PinCollector PRs 275 and 276, monthly release sheets can have their own vision reader. The manifest
  # points both ConfigMaps (Read again in the API, the nightly import job) at nardol's inference server at its
  # Tailscale address, http://100.66.174.88:8000/v1, model "default", no key (not 10.0.0.118: these Pods run on minas,
  # where 10.0.0.0/20 is the other site's LAN). Suggest details keeps inference.orleans.io. The API takes plain http
  # only to a private or Tailscale IP and never sends a key over http. No migrations and no schema change, so an
  # image-only rollback to 377df45 is safe (that image ignores the two new settings). The model image is
  # the live base relabelled (model-service/ and seed-data/ unchanged; RootFS matches the live image's). Published by
  # hand on nardol; both revision labels and the API's baked build fingerprint were verified before rollout.
  #
  # Release 377df45 (the owner's "go for it", 2026-11-10): admin follow-ups, PinCollector PRs 271-274. SerpApi
  # searches this month (a monthly tally and an on-click check of SerpApi's account), "Run import now" with a table
  # of import runs (the nightly job writes its runs too), each release row's box on its sheet, and one admin page
  # context. Three additive migrations: 20261109_serpapi01 (detail_lookup_months), 20261109_imports01
  # (release_import_runs) and 20261110_sheetbox01 (pin_releases.sheet_box, nullable). An older image's migration Job
  # wouldn't know the new head, so a rollback is a fix forward or an explicit downgrade. The manifest now sets the
  # release feed source in the API's config too, so "Run import now" reads the same feed as the nightly job. The
  # model image is the live base relabelled (model-service/ and seed-data/ unchanged; RootFS matches). Published by
  # hand on nardol; both revision labels and the API's baked build fingerprint were verified before rollout.
  #
  # Release d003c9b (the owner's "go for it, do all your recs", 2026-11-09): the admin redesign, PRs 265-270. A
  # sidebar with counts and a Today page, the release workbench and settings, the catalog table with its drawer and bulk
  # edits, the review queues as workbenches, Suggestions as switches, find from anywhere, a phone layout, and the last
  # f-string admin page retired (Pico removed). The admin UI plus the server code behind it (the two readings of a
  # disputed sheet field kept in the row's flags JSON, hiding a single release row, the catalog's batched reads). No
  # migrations and no schema change, so an image-only rollback to 6053fd1 is safe. The model image is the live 6053fd1 model image's base with only its
  # revision label set to d003c9b (model-service/, seed-data/ and .dockerignore unchanged; RootFS matches the live
  # image's). Published by hand on nardol like 6053fd1; both revision labels and the API's baked build fingerprint
  # were verified before rollout. No gate changes.
  #
  # Earlier, still in force:
  # Release 6053fd1: PRs 256 (the app's "Found on" field removed), 257, 259 and 261 (monthly release sheets:
  # migrations 20261109_release01 to release03, the Releases admin page, public GET /releases, sheets read by the
  # reader's vision model with Tesseract as cross-check and fallback, Disney's art on release rows behind its own
  # switch, first real photos of a release pin held for an admin), 258, 260, 262 and 263 (the app's Home tab,
  # releases, cards and Your 2026 in pins) and 264 (sheet panels padded for the vision reader). The three migrations
  # only add tables and columns. An older image's migration Job wouldn't know the new head, so a rollback is a fix
  # forward or an explicit downgrade, not an image swap. Published by hand, not by the publish-images workflow
  # (GitHub Actions minutes are spent): buildx on nardol, linux/amd64. The API image was built from the archived tree
  # of 6053fd1, with provenance and SBOM. The model image is the live 4794a66 model image's base with only its
  # revision label set to 6053fd1, without a provenance attestation: model-service/, seed-data/ and .dockerignore are
  # byte-identical between 4794a66 and 6053fd1, and its RootFS matches the live image's. Both OCI revision labels and
  # the API's baked build fingerprint (read from the built image) were verified before rollout.
  #
  # releaseImport goes on in this release (the owner's "yes turn it on, and run it", 2026-11-09): it unsuspends the
  # release-import CronJob. The job fetches from the feed only while the fetch switch at /admin/ui/releases is on;
  # release art stays off there. Nothing it reads is public until an admin approves it. Rollback: turn the fetch
  # switch off (no deploy), or set releaseImport back to false.
  #
  # Step 3 keys (since 2026-11-06, the owner's "lets do step 3"): suggestDetailsKeys mounts SerpApi's key and the
  # reader gateway's own key from secrets/pin-collector.yaml into the API's runtime Secret. Mounting turns nothing on
  # by itself: outside lookups start only when an admin picks SerpApi and Qwen at /admin/ui/suggestions. Rollback:
  # pick "Our catalog only" there (no deploy), or set this back to false.
  staged = true;
  enabled = true;
  registryPullSecretReady = true;
  # true holds the API Deployment at zero replicas (storage cutover; see
  # docs/runbooks/minas-tirith/pin-collector-garage.md). Everything else stays up.
  apiMaintenance = false;
  # Feature gates (docs/runbooks/minas-tirith/pin-collector.md, "Enabling a release").
  notificationSender = true;
  collectorPages = true;
  # Deploy 4 of 4: stardust and Plus on for everyone during the beta (the owner's go,
  # 2026-10-27), after the refresh Job completed and its report was read. Plus is free to
  # join while plus_beta_open stays at its default (true). The conversion Job is removed.
  stardust = true;
  plus = true;
  # Suggest details' outside lookups (PinCollector Step 3): mounts SerpApi's, SearchApi's and the
  # reader's keys into the API. Set it in the release that first pins an image with Step 3, after
  # all three keys are in secrets/pin-collector.yaml (docs/runbooks/minas-tirith/pin-collector.md;
  # SearchApi's since PinCollector #279). The keys alone turn nothing on: the admin chooses the
  # provider (SearchApi or SerpApi) and Qwen at /admin/ui/suggestions.
  suggestDetailsKeys = true;
  # PinCollector's monthly release sheets: unsuspends the release-import CronJob. Needs suggestDetailsKeys (the
  # reader's key). The feed fetch has its own switch at /admin/ui/releases (docs/runbooks/minas-tirith/pin-collector.md).
  releaseImport = true;
  # Home's Pin news from publishers' feeds: unsuspends the news-import CronJob (every 3 hours). Needs suggestDetailsKeys
  # (the reader's key). It reads nothing until the master switch at /admin/ui/releases/news is on, and a source can't be
  # switched on before its robots and terms check is written down (docs/runbooks/minas-tirith/pin-collector.md).
  # Rollback: the master switch, or set this back to false.
  newsImport = true;
  # Account mail (password reset codes): mounts Resend's key into the API and sends from
  # PinStellation <no-reply@pinstellation.com>, the domain verified in Resend. The key has Sending
  # access to pinstellation.com only. Needs resend_api_key in secrets/pin-collector.yaml first
  # (docs/runbooks/minas-tirith/pin-collector.md). Rollback: set this back to false; reset codes then
  # answer "unavailable", as before.
  accountMail = true;
  # Sign in with Apple's key (.p8): mounts it into the API with team N449SAELDJ and appleKeyId, so
  # the server keeps Apple refresh tokens and revokes them on account deletion. Sign-in works without
  # it. The owner made the key (Key ID LBD48KD2N3, app.pinstellation) and added apple_sign_in_key to
  # secrets/pin-collector.yaml on 2026-11-11. Rollback: set this back to false; sign-in keeps working.
  appleSignInKey = true;
  appleKeyId = "LBD48KD2N3";
  # Photo screening (PinCollector #290), OpenAI's moderation check of catalog photos, in three steps.
  # photoScreeningKey mounts openai_api_key (a restricted key in its own OpenAI project, with a spend
  # cap; the owner added it on 2026-11-12) into the API alone, so the owner can dry-run the catalog in
  # the API Pod (python -m app.maintenance.check_catalog_photos, which writes nothing). The key alone
  # checks no photo, though the privacy page names OpenAI from then on. photoChecks turns the check on
  # for everyone after that dry run; the contract refuses it without the key. peopleReportsAlert mounts
  # the Healthchecks ping URL of "PinCollector people reports"; the contract refuses it until
  # people_reports_alert_url is in secrets/pin-collector.yaml. Rollback: set each back to false.
  # Dry run 2026-11-12 on release 8adcac7 (through the container's entrypoint): 150 of 150 catalog
  # photos checked, 0 would be flagged, 0 unreadable; highest score violence 0.43. Checks on.
  photoScreeningKey = true;
  photoChecks = true;
  peopleReportsAlert = false;
  gitRevision = "b963c92e2952ae027e321913914b677e507503be";
  apiImage = "ghcr.io/edgarsaldivar/pin-collector-api@sha256:a1fc9b9c17d41e2dc61d3ed1fbfc30e581691e5651827a32ffa3d14d4d69e4ab";
  apiImageRevision = "b963c92e2952ae027e321913914b677e507503be";
  modelImage = "ghcr.io/edgarsaldivar/pin-collector-model-service@sha256:1203a6f3cb76e3a03fac63e8b3e9865d82cfa3072864458007246f74f0996126";
  modelImageRevision = "b963c92e2952ae027e321913914b677e507503be";
}
