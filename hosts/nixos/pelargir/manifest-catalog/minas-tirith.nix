{
  minasTraefik,
  pinCollectorManifest,
}:
# Delivered from pelargir like everything else — agents have no auto-deploy
# directory — but grouped so it is obvious whose workloads these are.
#
# ⚠️ ORDERING: k3s applies this directory in FILENAME order, so
# `minas-audiobookshelf.yaml` is applied BEFORE `minas-namespaces.yaml` and fails
# its first pass with `namespaces "books" not found`. k3s then retries ~15 s later,
# after the namespace exists, and succeeds. Observed on the first deploy; it is
# self-healing, not a fault.
#
# It is NOT fixed by renaming the namespace file to sort first, deliberately.
# Renaming creates a new file and orphans the old one, whose AddOn record still
# owns the `books` Namespace through its objectset annotations. Deleting that AddOn
# to tidy up could garbage-collect the namespace AND every workload inside it.
# Expect one ApplyManifestFailed warning per new namespace on first apply and read
# it as ordering, not breakage.
[
  {
    name = "minas-namespaces.yaml";
    path = ../../minas-tirith/manifests/namespaces.yaml;
  }
  # New basename, deliberately sorting after minas-namespaces.yaml: every object in
  # this file is namespaced and k3s AddOn ownership makes later renames unsafe.
  {
    name = "minas-workload-authentik.yaml";
    path = ../../minas-tirith/manifests/authentik.yaml;
  }
  {
    name = "minas-audiobookshelf.yaml";
    path = ../../minas-tirith/manifests/audiobookshelf.yaml;
  }
  {
    name = "minas-nvidia-device-plugin.yaml";
    path = ../../minas-tirith/manifests/nvidia-device-plugin.yaml;
  }
  {
    name = "minas-komga.yaml";
    path = ../../minas-tirith/manifests/komga.yaml;
  }
  {
    name = "minas-palworld.yaml";
    path = ../../minas-tirith/manifests/palworld.yaml;
  }
  # Tier A independent services, not a wave; each can cut over separately.
  {
    name = "minas-kavita.yaml";
    path = ../../minas-tirith/manifests/kavita.yaml;
  }
  {
    name = "minas-calibre.yaml";
    path = ../../minas-tirith/manifests/calibre.yaml;
  }
  # flaresolverr REPLACES its entry in minas-docker-bridges.yaml. Both files
  # are applied from this directory, so the bridge objects must be deleted by
  # hand at cutover — auto-deploy does not prune what a manifest stops
  # declaring, it only stops re-asserting it.
  {
    name = "minas-flaresolverr.yaml";
    path = ../../minas-tirith/manifests/flaresolverr.yaml;
  }
  # Atomic `media` wave. These files remain scaled to zero until the hand-run
  # cutover stops the corresponding docker containers and starts the group.
  {
    name = "minas-tautulli.yaml";
    path = ../../minas-tirith/manifests/tautulli.yaml;
  }
  {
    name = "minas-overseerr.yaml";
    path = ../../minas-tirith/manifests/overseerr.yaml;
  }
  {
    name = "minas-prowlarr.yaml";
    path = ../../minas-tirith/manifests/prowlarr.yaml;
  }
  {
    name = "minas-sonarr.yaml";
    path = ../../minas-tirith/manifests/sonarr.yaml;
  }
  {
    name = "minas-radarr.yaml";
    path = ../../minas-tirith/manifests/radarr.yaml;
  }
  {
    name = "minas-lidarr.yaml";
    path = ../../minas-tirith/manifests/lidarr.yaml;
  }
  {
    name = "minas-animearr.yaml";
    path = ../../minas-tirith/manifests/animearr.yaml;
  }
  {
    name = "minas-cleanuparr.yaml";
    path = ../../minas-tirith/manifests/cleanuparr.yaml;
  }
  {
    name = "minas-maintainerr.yaml";
    path = ../../minas-tirith/manifests/maintainerr.yaml;
  }
  {
    name = "minas-wrapperr.yaml";
    path = ../../minas-tirith/manifests/wrapperr.yaml;
  }
  {
    name = "minas-shelfmark.yaml";
    path = ../../minas-tirith/manifests/shelfmark.yaml;
  }
  # jellyfin is a StatefulSet, not a Deployment, because its database can only be
  # dumped while it is NOT running — see docs/runbooks/minas-tirith/backup-restore.md. The quiesce
  # ServiceAccount/Role/RoleBinding/CronJob that deletes `jellyfin-0` nightly is a
  # separate file so the RBAC is reviewable on its own.
  {
    name = "minas-jellyfin.yaml";
    path = ../../minas-tirith/manifests/jellyfin.yaml;
  }
  {
    name = "minas-jellyfin-quiesce.yaml";
    path = ../../minas-tirith/manifests/jellyfin-quiesce.yaml;
  }
  # plex. MIGRATED 2026-08-08 — live at one replica with its own Service. The
  # staging comment that stood here (replicas 0, no Service, bridge untouched
  # until cutover) described the pre-cutover state and is kept only as history.
  #
  # ⚠️ ORDERING, corrected by cross-review — an earlier version of this comment had
  # it exactly backwards. `minas-docker-bridges.yaml` sorts BEFORE
  # `minas-plex.yaml`, so the bridge AddOn is applied FIRST. On the cutover deploy
  # that removes the bridge entry, that AddOn therefore PRUNES the `plex` Service
  # before this file's AddOn recreates it: a delete-and-recreate across two owners,
  # not an in-place patch. The cutover manifest pins the existing ClusterIP so the
  # recreation cannot hand out a new address.
  {
    name = "minas-plex.yaml";
    path = ../../minas-tirith/manifests/plex.yaml;
  }
  # readmeabook carries its OWN alias objects rather than adding them to
  # minas-docker-bridges.yaml: an ExternalName for `prowlarr` (which lives in the
  # `media` namespace) and a bridge for `gluetun`, which was selectorless while
  # gluetun was still a docker container and is now backed by the netns Pod.
  # Keeping them in this file means the workload and the names it depends on are
  # added and removed together, and it avoids a second AddOn owning objects in the
  # `books` namespace.
  {
    name = "minas-readmeabook.yaml";
    path = ../../minas-tirith/manifests/readmeabook.yaml;
  }
  # tracearr needs no alias objects: its only bare-name edge is tautulli, which is
  # already in `media`. Its SECRET is NOT delivered from here — it is rendered from
  # sops to tmpfs and applied by k3s-apply-secrets, because this directory is
  # unencrypted disk.
  {
    name = "minas-tracearr.yaml";
    path = ../../minas-tirith/manifests/tracearr.yaml;
  }
  # ⚠️ THE NAME IS NOW A LIE, AND DELIBERATELY SO. This file no longer holds any
  # bridge to a docker container — docker runs zero containers. What it still owns
  # is the `deluge-books` and `deluge-vpn` Services, which began as selectorless
  # bridges and were updated IN PLACE to selector-backed Services when those
  # workloads migrated (2026-08-09).
  #
  # They stay here because moving a Service to its workload's own manifest is a
  # delete-and-recreate ACROSS TWO AddOns: the AddOn applied later prunes what the
  # other just created, giving a window with no Service and a new ClusterIP.
  # Updating in place is one patch on one object with one owner. The filename is
  # the price of that, and renaming it would be the very delete-and-recreate this
  # arrangement exists to avoid. See the file's own header for the full reasoning.
  {
    name = "minas-docker-bridges.yaml";
    path = ../../minas-tirith/manifests/docker-bridges.yaml;
  }
  # ---------------------------------------------------------------------------
  #  VPN-gated workloads — see docs/architecture/k3s.md
  # ---------------------------------------------------------------------------
  # ⛔ THE `minas-vpn-` PREFIX IS LOAD-BEARING. DO NOT "TIDY" IT TO `minas-deluge-`.
  #
  # k3s applies this directory in FILENAME order and never prunes on removal. The
  # natural name `minas-deluge-books.yaml` sorts BEFORE `minas-docker-bridges.yaml`
  # (which owns the `deluge-books`/`deluge-vpn` Services and their `-docker`
  # EndpointSlices) and before `minas-readmeabook.yaml` (which owns the `gluetun`
  # bridge). The old owner would then apply LAST on the cutover deploy and PRUNE the
  # Service this file had just created — the Pods come up healthy and their Services
  # vanish, which looks like a manifest that is simply not working.
  #
  # `minas-vpn-*` sorts after both. Found by cross-review before the filenames were
  # frozen; renaming later means a delete-and-recreate across two AddOns.
  #
  # MIGRATED 2026-08-09 and live at one replica. Its Service is the in-place-updated
  # one in minas-docker-bridges.yaml above, not a Service declared here. The staging
  # note that stood here (replicas 0, no Service, everything landing together at
  # cutover) described the pre-cutover plan and is kept only as history.
  {
    name = "minas-vpn-deluge-books.yaml";
    path = ../../minas-tirith/manifests/vpn-deluge-books.yaml;
  }
  # Same `minas-vpn-` prefix, same reason: it must sort AFTER
  # minas-docker-bridges.yaml, which owns the deluge-vpn Service and its
  # deluge-vpn-docker EndpointSlice until the cutover updates them in place.
  {
    name = "minas-vpn-deluge-vpn.yaml";
    path = ../../minas-tirith/manifests/vpn-deluge-vpn.yaml;
  }
  # The books netns trio as ONE Pod. Same `minas-vpn-` prefix and the same reason: it
  # must sort AFTER minas-readmeabook.yaml, which owns the `gluetun` Service and its
  # gluetun-docker EndpointSlice until the cutover updates them in place.
  {
    name = "minas-vpn-books-netns.yaml";
    path = ../../minas-tirith/manifests/books-netns.yaml;
  }
  # nextcloud: app + database as SEPARATE workloads. No bridge to take over (it was
  # never bridged — it is reached only through traefik), so no sort-order hazard here;
  # the `minas-nextcloud-` name is chosen for grouping, not ordering.
  {
    name = "minas-nextcloud.yaml";
    path = ../../minas-tirith/manifests/nextcloud.yaml;
  }
  # immich app + PostgreSQL + disposable Redis, all staged inert at replicas 0.
  {
    name = "minas-immich.yaml";
    path = ../../minas-tirith/manifests/immich.yaml;
  }
  # Permanently managed and inert while staged=false. The frozen basename and
  # zero-replica rendering make deactivation fail closed without deleting PVCs.
  {
    name = "minas-pin-collector.yaml";
    path = pinCollectorManifest;
  }
  # This basename must remain distinct from k3s's packaged `traefik.yaml`.
  # The ingress replacement is staged inert; raising replicas is a gated cutover.
  {
    name = "minas-traefik.yaml";
    # ⛔ GENERATED, not copied. The Cloudflare edge ranges are declared exactly
    # once, in ../cloudflare-ranges.nix, and substituted by
    # ../minas-traefik-manifest.nix -- which checks/cloudflare-ranges.nix imports
    # too, so there is one substitution rather than a production one and a
    # verification one that can drift apart.
    #
    # The `name` above is unchanged, so the k3s AddOn identity -- derived from the
    # installed basename and FROZEN -- is untouched. Only the CONTENT is produced
    # differently.
    #
    # ⚠️ SCOPE OF THE "unchanged" CLAIM -- corrected after cross-review, 2026-08-18.
    #
    # The Cloudflare substitution alone is object-neutral: every non-comment byte
    # matches what the literal list produced, trustedIPs included. Only comments
    # were added, so the checksum changes and k3s re-applies once.
    #
    # ⛔ That is NOT true of this manifest across the whole branch. The same file
    # also moves traefik's file-provider hostPath from the old docker checkout to
    # /usr/local/etc/traefik. That IS a spec.template change on a singleton with
    # `strategy: Recreate`, i.e. a full ingress rollout for 26 hostnames -- not a
    # no-op. It was executed deliberately during the relocation and has already
    # happened on the live host.
    #
    # An earlier version of this comment said the applied object was unchanged
    # full stop, which would tell an operator that no rollout occurs. Per
    # AGENTS.md, confirm with `kubectl diff` before touching traefik rather than
    # trusting a comment about it.
    path = minasTraefik;
  }
]
