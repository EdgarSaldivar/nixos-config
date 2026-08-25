# Roadmap

Open work only. Finished work is deleted from this file and remains available in
git history. Source owns configured facts; runbooks own procedures; observations
about hardware or runtime must be collected before closing the items marked ⏳.

Last source audit: **2026-08-24**.

## Backup integrity and artifact lifecycle

- ⏳ **Verify that the backup degraded marker clears naturally.** After a nightly
  `backup-root-data` run, inspect `systemctl status backup-root-data` and confirm
  `/var/lib/backup-root-data.degraded` is absent. Do not delete the marker by hand:
  the test is that the successful path in
  [`backup-root-data.sh`](hosts/nixos/minas-tirith/scripts/backup-root-data.sh)
  removes the stale names `infra-postgres-1`, `immich-postgres14`, and
  `nextcloud-db`.

- **Make an explicit retirement decision for the legacy `infra-postgres-1`
  artifact.** Before deletion, verify through the consumer/restore path that
  `k8s-pin-collector-postgres.sql.gz` is a fresh, usable successor. The old name is
  the PinCollector database, so its relationship is not evident from the filename.

- **Stop treating Shelfmark WAL sidecars as dump inputs.** Exclude
  `_usr_local_etc_shelfmark_config_users.db-wal` and
  `_usr_local_etc_shelfmark_config_users.db-shm`; they are SQLite `-wal` and `-shm`
  sidecars, not independent databases.

- **Add Tracearr freshness and exact-format validation.** Include
  `k8s-media-tracearr.dump` in the age check, and make the never-created check map
  every declared database to its one expected extension instead of accepting any
  of `.sql.gz`, `.sql.gz.age`, or `.dump`. Tracearr must remain custom-format
  because its `.sql.gz` form is not restorable; keep the tested procedure in the
  [database backup/restore runbook](docs/runbooks/minas-tirith/backup-restore.md).

- **Define a general orphan-artifact retirement rule.** When a declared backup
  source disappears or its derived name changes, report the orphan, require an
  explicit successor/restore verification, and retire it through an explicit list.
  Do not add more one-off filename exceptions or silently prune possible last
  copies.

## Cluster lifecycle and networking

- **Raise all four Osgiliath-pinned workloads only when Osgiliath joins the
  cluster.** After observing the node registered and its required paths/devices
  available, change `replicas: 0` to `replicas: 1` for
  [home-assistant](hosts/nixos/osgiliath/manifests/home-assistant.yaml),
  [frigate](hosts/nixos/osgiliath/manifests/frigate.yaml),
  [mosquitto](hosts/nixos/osgiliath/manifests/mosquitto.yaml), and
  [edge](hosts/nixos/osgiliath/manifests/edge.yaml). Do not raise any of them early;
  see the [k3s architecture notes](docs/architecture/k3s.md).

- **Decide Cloudflare `trustedIPs` IPv6 behavior deliberately.**
  The shared source contains seven IPv6 ranges, while Traefik's
  `forwardedHeaders.trustedIPs` is
  generated from the 15 IPv4 ranges only. Decide whether the origin can receive
  Cloudflare traffic over IPv6 and either add the IPv6 ranges with an explicit
  behavior change or document the IPv4-only boundary. Source:
  [`cloudflare-ranges.nix`](hosts/nixos/pelargir/cloudflare-ranges.nix).

- **Add namespace default-deny NetworkPolicy.** Finish and validate default-deny
  plus required allows for `books`, `media`, `games`, `nextcloud`, and `immich`.
  Start from the checked-in
  [NetworkPolicy experiments](experiments/network-policies/README.md), and verify
  ingress, DNS, storage, and cross-namespace dependencies before promotion.

- **Design graceful Kubernetes and PostgreSQL shutdown ordering.** Stop or drain
  the kubelet's database Pods and obtain clean PostgreSQL shutdown before the ZFS
  pools disappear during reboot, rollback, or shutdown. Keep the `verify-pgdata`
  gate fail-closed; never loosen it to guess that a stale `postmaster.pid` is safe.
  Recovery remains in the
  [unclean-shutdown runbook](docs/runbooks/minas-tirith/postgres-unclean-shutdown.md).

- **Add ingress-level monitoring for 502-class failures.** Run a behavioral probe
  against the recorded ingress baseline so a healthy-looking application Pod with
  an unavailable database still alerts. A workload-count or sandbox-readiness
  check is not equivalent. Use the source acceptance command
  `hosts/nixos/minas-tirith/scripts/ingress-acceptance.py` as the behavioral model.

- **Decide whether to restore per-container readiness coverage.** The current
  sandbox-state check cannot observe a container whose Kubernetes readiness probe
  is failing. Either add a readiness-aware check or explicitly accept the gap;
  retain service-specific behavioral checks for the VPN tunnels.

## Credentials

- **Revoke or regenerate the exposed Cloudflare global API key.** First check for
  consumers outside this repository, because rotating the global identity can
  break them. Deletion of the legacy plaintext file did not revoke the issuer-side
  credential. Do not rotate the scoped `traefik_cloudflare_dns_api_token` as a
  substitute for revoking the global key.

- **Rotate `palworld_admin_password` as a reused password.** Update the secret at a
  controlled maintenance window without changing or weakening any other
  [Palworld settings](hosts/nixos/minas-tirith/manifests/palworld.yaml). If the same
  string is used anywhere outside this fleet, change it there too: reuse across
  unrelated roles is the larger exposure.

- **Rotate the PIA OpenVPN credentials and verify one tunnel at a time.** Update
  `pia_openvpn_username` and `pia_openvpn_password`, then restart and verify
  `deluge-vpn`, `deluge-books`, and the books-netns gluetun sequentially. Between
  each restart, prove both tunnel establishment and non-local egress; never restart
  all three together because they share the PIA identity. Consumers are declared in
  [vpn-deluge-vpn.yaml](hosts/nixos/minas-tirith/manifests/vpn-deluge-vpn.yaml),
  [vpn-deluge-books.yaml](hosts/nixos/minas-tirith/manifests/vpn-deluge-books.yaml),
  and [books-netns.yaml](hosts/nixos/minas-tirith/manifests/books-netns.yaml).

- ⏳ **Verify password-manager recovery is in a separate failure domain.** Confirm
  its Emergency Kit can be recovered without this Mac. If the fleet becomes
  multi-operator, add a second human sops recipient so recovery does not depend on
  one administrator being available.

## Automation and retained data

- **Evaluate deploy-rs.** Test whether its activation confirmation and automatic
  rollback fit this fleet's native-on-host rebuild procedure and failure modes,
  especially a remote machine that may require a physical visit. Adoption must not
  bypass the repository's rsync, build/test, second-rsync, and switch sequence.

- **Add Renovate.** Configure input-update PRs with the existing evaluation checks
  as gates; keep update scope reviewable and avoid unattended activation.

- ⏳ **Decide deliberately whether to preserve or discard
  `/var/lib/resource-samples/samples.csv`.** It is an empirical input to manifest
  resource requests and is outside the backup source set. If retained, add it to a
  named durable source and test restoration; if discarded, record that the samples
  must be recollected before retuning requests.

## Wolf audio verification

Treat these as three unverified assumptions to observe, not diagnoses. Start with
liveness checks that fail loudly when the expected PulseAudio module or source is
absent. Use the [client microphone runbook](docs/runbooks/nardol/client-microphone.md)
and keep [`nardol-gaming-contract.nix`](checks/nardol-gaming-contract.nix) aligned
with any intentional mode or mount change.

- **Observe how `/etc/cont-init.d/95-nardol-client-mic.sh` is consumed.** Determine
  whether the entrypoint sources or executes the mode-`0444` script and verify that
  `PULSE_SOURCE` reaches the Steam container. If execution is the contract, account
  for the missing executable bit and the fact that exports from a child process do
  not reach its parent.

- **Observe `nardol-vban-microphone` access to `/run/wolf/pulse-socket`.** Verify
  socket ownership/mode and whether libpulse needs `$HOME/.config/pulse/cookie`
  under `DynamicUser` plus `ProtectHome`; surface permission failures directly
  instead of relying on a restart loop.

- **Observe recovery after Wolf's internal PulseAudio restarts.** Verify the null
  sink, remap source, and `vban_receptor` target are re-created even when
  `docker-wolf.service` itself does not restart. Add a liveness assertion that
  makes missing modules or sources visible rather than leaving an active unit with
  no audio.

## Pelargir control-plane architecture

- **Decide the control-plane failure-domain policy.** Pelargir is declared as the
  sole k3s control plane and manifest delivery node. Decide whether loss of that
  failure domain may leave Minas serving best-effort without API, reconciliation,
  cert-manager, restart, or reschedule.

- Do not treat a two-member control plane as high availability; it has no quorum
  tolerance. If Minas must survive a long Pelargir outage, evaluate one cluster per
  house with a second control plane and duplicated operations. Record the chosen
  availability contract in [the architecture document](docs/architecture/k3s.md).

## Verify on the hardware

These tasks require observations; declarations in Nix do not settle them.

- ⏳ **Nightly database backup:** observe a completed `backup-root-data` run, its
  stamp, the absence or contents of `/var/lib/backup-root-data.degraded`, and fresh,
  nontrivial dump sizes. Follow the
  [backup/restore runbook](docs/runbooks/minas-tirith/backup-restore.md) far enough
  to prove the artifacts through their consumer path.

- ⏳ **Pelargir off-host restic:** run `restic-minas snapshots`, inspect the latest
  snapshot age, and perform a scratch restore according to the
  [Pelargir backup](docs/runbooks/pelargir/backup.md) and
  [restore-drill](docs/runbooks/pelargir/restore-drill.md) runbooks.

- ⏳ **Nardol cold boot and unlock:** at the machine, observe a cold boot with the
  initrd unlock changes, measure whether `keyFileTimeout = 10` is sufficient across
  every USB port, verify slot 0, perform all nine unlock drills in the
  [Nardol unlock-operations runbook](docs/runbooks/nardol/unlock-operations.md#13-unlock-drills), and resolve the USB
  stick being the only copy of that key.

- ⏳ **Rescue microSD:** rehearse the rollback procedure on the hardware; a written
  procedure alone is not proof.

- ⏳ **Pi RTC battery:** commission and observe the battery before setting
  `dtparam=rtc_bbat_vchg`.

- ⏳ **Storage pool and disk health:** collect the completed scrub result and fresh
  SMART counters, including any `storage:<0x41080>` error and Scrutiny latch/counter
  behavior. Do not infer completion from an in-progress scrub.

## Physical risks

- **No offsite copy:** about 98 TB is protected only by storage on the same machine;
  plan a failure domain that covers fire, theft, and a shared PSU event.

- **Installed HBA:** the HBA remains physically installed, so preserve and verify
  the [install runbook's](docs/runbooks/minas-tirith/install.md) software
  destruction fence and the warnings in
  [`disko.nix`](hosts/nixos/minas-tirith/disko.nix) before disk work; software is
  the primary fence, not a backup to physical removal.

- **No UPS:** provide power protection for the host; the root NVMe has no power-loss
  protection.

- **Disk serial `9JH1LNDT`:** collect current scrub and SMART observations, then
  replace this WD Ultrastar DC HC530 (`wwn-0x5000cca258ced1ea`) at the next
  convenient window unless the evidence requires escalation. Identify it only by
  serial or WWN, never `/dev/sdX`, because discovery-order names move.

## Maintenance constraints

- ⛔ **Keep `dungeon.saldivar.io` DNS intentionally.** The project is retained for
  future use; do not delete its DNS record merely because no route is expected.
  Retire it only after an explicit operator decision.

- Keep fail-closed imports: do not add `lib/try-import.nix`-style optional hardware
  or disko imports. Do not add an empty global overlay/`packages/` layer, broad
  nixpkgs-unstable, or mutually exclusive machine-class enums; use individual
  packages and composable capability/role modules as described in
  [`modules/README.md`](modules/README.md).

- Do not remove `hardware.nvidia-container-toolkit` as Docker cleanup; it provides
  the CDI spec used by k3s GPU workloads. Retain the SQLite quiesce capability until
  a Kubernetes replacement with equivalent stop/trap/restart safety is designed.
