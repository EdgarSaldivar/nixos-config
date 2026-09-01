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
# after the namespace exists, and succeeds. This retry is self-healing ordering,
# not a fault.
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
  # This frozen basename sorts after minas-namespaces.yaml: every object in this
  # file is namespaced and k3s AddOn ownership makes later renames unsafe.
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
  # Independent service entries; their frozen basenames are not wave ordering.
  {
    name = "minas-kavita.yaml";
    path = ../../minas-tirith/manifests/kavita.yaml;
  }
  {
    name = "minas-calibre.yaml";
    path = ../../minas-tirith/manifests/calibre.yaml;
  }
  # flaresolverr owns its Service rather than sharing ownership with
  # minas-docker-bridges.yaml. Auto-deploy does not prune objects merely because a
  # manifest stops declaring them, so ownership transitions require explicit
  # removal from the old AddOn.
  {
    name = "minas-flaresolverr.yaml";
    path = ../../minas-tirith/manifests/flaresolverr.yaml;
  }
  # `media` workloads retain their frozen AddOn basenames and declare their durable
  # replica counts in their source manifests.
  {
    name = "minas-tautulli.yaml";
    path = ../../minas-tirith/manifests/tautulli.yaml;
  }
  # ⚠️ THE NAME IS DELIBERATELY HISTORICAL. This AddOn delivers SEERR, which
  # replaced the deprecated `ghcr.io/sct/overseerr`. The installed basename is
  # frozen, and the Deployment/Service names inside it cannot be renamed either —
  # see the manifest's own header for why.
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
  # plex declares one replica and owns its Service.
  #
  # ⚠️ ORDERING: `minas-docker-bridges.yaml` sorts BEFORE `minas-plex.yaml`, so a
  # bridge-owner removal is applied before this AddOn recreates the `plex` Service.
  # That ownership transfer is delete-and-recreate, not an in-place patch; the
  # Service pins its ClusterIP so recreation cannot hand out a new address.
  {
    name = "minas-plex.yaml";
    path = ../../minas-tirith/manifests/plex.yaml;
  }
  # readmeabook carries its OWN alias objects rather than adding them to
  # minas-docker-bridges.yaml: an ExternalName for `prowlarr` (which lives in the
  # `media` namespace) and the selector-backed `gluetun` Service for the netns Pod.
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
  # ⚠️ THE NAME IS DELIBERATELY HISTORICAL. This file owns the selector-backed
  # `deluge-books` and `deluge-vpn` Services under their original AddOn identity.
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
  # bridge). The old owner would then apply LAST and PRUNE the
  # Service this file had just created — the Pods come up healthy and their Services
  # vanish, which looks like a manifest that is simply not working.
  #
  # `minas-vpn-*` sorts after both. Found by cross-review before the filenames were
  # frozen; renaming later means a delete-and-recreate across two AddOns.
  #
  # The Deployment declares one replica. Its selector-backed Service remains under
  # the original owner in minas-docker-bridges.yaml rather than being declared here.
  {
    name = "minas-vpn-deluge-books.yaml";
    path = ../../minas-tirith/manifests/vpn-deluge-books.yaml;
  }
  # Same `minas-vpn-` prefix, same reason: it must sort AFTER
  # minas-docker-bridges.yaml, which owns the deluge-vpn Service; preserving that
  # owner avoids an unsafe cross-AddOn move.
  {
    name = "minas-vpn-deluge-vpn.yaml";
    path = ../../minas-tirith/manifests/vpn-deluge-vpn.yaml;
  }
  # The books netns trio as ONE Pod. Same `minas-vpn-` prefix and the same reason: it
  # must sort AFTER minas-readmeabook.yaml, which owns the `gluetun` Service;
  # preserving that owner avoids an unsafe cross-AddOn move.
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
  # immich app + PostgreSQL + disposable Redis; all three Deployments declare one
  # replica in the source manifest.
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
  # This basename must remain distinct from k3s's packaged `traefik.yaml`. The
  # pinned ingress singleton declares one replica; any replacement or rollback is
  # gated because Recreate causes a full public-ingress outage.
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
    # The Cloudflare substitution is object-neutral: it emits the declared ranges
    # without changing the applied fields. This does not make unrelated edits to
    # the generated manifest object-neutral: any `spec.template` change on this
    # Recreate singleton causes a full ingress outage for 26 hostnames. Per
    # AGENTS.md, confirm with `kubectl diff` before touching traefik.
    path = minasTraefik;
  }
]
