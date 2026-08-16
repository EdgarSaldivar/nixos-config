# Copy immutable manifests from the store into k3s' auto-deploy directory. k3s
# continuously reconciles it, so replacing files is enough.
#
# Secret manifests are NOT delivered here — they are applied from tmpfs by
# k3s-apply-secrets (see secrets.nix), because this directory is persistent disk
# on a host with no full-disk encryption.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  pinCollectorReleaseContract = import ../minas-tirith/pin-collector-release-contract.nix {
    inherit lib;
  };
  pinCollectorRelease = pinCollectorReleaseContract.assertValid (
    import ../minas-tirith/pin-collector-release.nix
  );
  # Keep the manifest permanently owned after its first activation. k3s does not
  # prune a removed auto-deploy file, so `staged = false` must render an inert
  # object set rather than dropping the file and leaving the previous release live.
  pinCollectorInertImage =
    "registry.invalid/pin-collector/inert@sha256:${lib.concatStrings (lib.replicate 64 "0")}";
  pinCollectorApiImage =
    if pinCollectorRelease.staged then pinCollectorRelease.apiImage else pinCollectorInertImage;
  pinCollectorModelImage =
    if pinCollectorRelease.staged then pinCollectorRelease.modelImage else pinCollectorInertImage;
  pinCollectorApiDigest =
    if pinCollectorRelease.staged
    then lib.removePrefix "ghcr.io/edgarsaldivar/pin-collector-api@sha256:" pinCollectorApiImage
    else lib.concatStrings (lib.replicate 64 "0");
  pinCollectorGitRevision =
    if pinCollectorRelease.staged
    then pinCollectorRelease.gitRevision
    else lib.concatStrings (lib.replicate 40 "0");
  pinCollectorManifest = pkgs.replaceVars ../minas-tirith/manifests/pin-collector.yaml.in {
    apiImage = pinCollectorApiImage;
    modelImage = pinCollectorModelImage;
    gitRevision = pinCollectorGitRevision;
    statefulReplicas = if pinCollectorRelease.staged then "1" else "0";
    apiReplicas = if pinCollectorRelease.enabled then "1" else "0";
    modelReplicas = if pinCollectorRelease.enabled then "1" else "0";
    migrationSuspended = if pinCollectorRelease.enabled then "false" else "true";
    migrationJobName = "pin-collector-migrate-${builtins.substring 0 12 pinCollectorApiDigest}";
  };
  # ---------------------------------------------------------------------------
  #  A.2 — manifests grouped BY OWNING HOST
  # ---------------------------------------------------------------------------
  # Everything is still delivered from pelargir's single auto-deploy directory —
  # agents have none — so this is ORGANISATIONAL separation, not blast-radius
  # isolation. A malformed manifest for any host still reaches the cluster
  # through this one server, and a Nix activation reapplies every file.
  #
  # It earns its keep when minas' ~32 manifests arrive in Phase 3: without
  # grouping, one flat list would carry three hosts' workloads with no way to
  # see whose is whose, and the ownership map would be equally undifferentiated.
  #
  # ⚠️  FILENAMES ARE FROZEN. Renaming an entry does not rename anything in the
  # auto-deploy directory — it creates a NEW file and leaves the old one behind
  # forever, still being applied, because k3s never prunes. The `osgiliath-`
  # prefixes below are therefore kept exactly as they were first written, even
  # though the grouping now makes them redundant.
  manifestsByHost = {
    # Cluster-scoped objects belong to no single host. They are grouped separately so
    # the ownership map does not imply that e.g. a StorageClass is pelargir's, and so
    # they are not mistakenly removed with a host's workloads.
    cluster = [
      { name = "storage.yaml";     path = ./manifests/storage.yaml; }
      # NOT "coredns.yaml" — that is k3s's own packaged filename. Reusing it would
      # collide in this directory, and if --disable=coredns is ever set the disable
      # matches by BASENAME permanently, turning the file into a delete-on-sight trap.
      { name = "coredns-ha.yaml";  path = ./manifests/coredns-ha.yaml; }
      { name = "coredns-custom.yaml"; path = corednsCustom; }
    ];
    pelargir = [
      { name = "namespace.yaml";      path = ./manifests/namespace.yaml; }
      { name = "mosquitto.yaml";      path = ./manifests/mosquitto.yaml; }
      { name = "zigbee2mqtt.yaml";    path = ./manifests/zigbee2mqtt.yaml; }
      { name = "home-assistant.yaml"; path = ./manifests/home-assistant.yaml; }
      { name = "ddns.yaml";           path = ./manifests/ddns.yaml; }
      { name = "ingress.yaml";        path = ./manifests/ingress.yaml; }
    ];
    osgiliath = [
      { name = "osgiliath-namespace.yaml";      path = ../osgiliath/manifests/namespace.yaml; }
      { name = "osgiliath-frigate.yaml";        path = ../osgiliath/manifests/frigate.yaml; }
      { name = "osgiliath-home-assistant.yaml"; path = ../osgiliath/manifests/home-assistant.yaml; }
      { name = "osgiliath-mosquitto.yaml";      path = ../osgiliath/manifests/mosquitto.yaml; }
      { name = "osgiliath-edge.yaml";           path = ../osgiliath/manifests/edge.yaml; }
    ];
    # Phase 2 onward. Delivered from pelargir like everything else — agents have no
    # auto-deploy directory — but grouped so it is obvious whose workloads these are.
    #
    # ⚠️  ORDERING: k3s applies this directory in FILENAME order, so
    # `minas-audiobookshelf.yaml` is applied BEFORE `minas-namespaces.yaml` and fails
    # its first pass with `namespaces "books" not found`. k3s then retries ~15 s later,
    # after the namespace exists, and succeeds. Observed on the first deploy; it is
    # self-healing, not a fault.
    #
    # It is NOT fixed by renaming the namespace file to sort first, deliberately.
    # Renaming does not rename anything in the auto-deploy directory — it creates a new
    # file and orphans the old one, whose AddOn record still owns the `books` Namespace
    # through its objectset annotations. Deleting that AddOn to tidy up could
    # garbage-collect the namespace AND every workload inside it. Trading a transient,
    # self-healing warning for a way to delete a live namespace is a bad trade.
    #
    # So: expect one ApplyManifestFailed warning per new namespace on first apply, and
    # read it as ordering, not breakage.
    minas-tirith = [
      { name = "minas-namespaces.yaml";    path = ../minas-tirith/manifests/namespaces.yaml; }
      # New basename, deliberately sorting after minas-namespaces.yaml: every object in
      # this file is namespaced and k3s AddOn ownership makes later renames unsafe.
      { name = "minas-workload-authentik.yaml"; path = ../minas-tirith/manifests/authentik.yaml; }
      { name = "minas-audiobookshelf.yaml"; path = ../minas-tirith/manifests/audiobookshelf.yaml; }
      { name = "minas-nvidia-device-plugin.yaml"; path = ../minas-tirith/manifests/nvidia-device-plugin.yaml; }
      { name = "minas-komga.yaml";          path = ../minas-tirith/manifests/komga.yaml; }
      { name = "minas-palworld.yaml";       path = ../minas-tirith/manifests/palworld.yaml; }
      # Tier A independent services, not a wave; each can cut over separately.
      { name = "minas-kavita.yaml";         path = ../minas-tirith/manifests/kavita.yaml; }
      { name = "minas-calibre.yaml";        path = ../minas-tirith/manifests/calibre.yaml; }
      # flaresolverr REPLACES its entry in minas-docker-bridges.yaml. Both files
      # are applied from this directory, so the bridge objects must be deleted by
      # hand at cutover — auto-deploy does not prune what a manifest stops
      # declaring, it only stops re-asserting it.
      { name = "minas-flaresolverr.yaml";   path = ../minas-tirith/manifests/flaresolverr.yaml; }
      # Atomic `media` wave. These files remain scaled to zero until the hand-run
      # cutover stops the corresponding docker containers and starts the group.
      { name = "minas-tautulli.yaml";        path = ../minas-tirith/manifests/tautulli.yaml; }
      { name = "minas-overseerr.yaml";       path = ../minas-tirith/manifests/overseerr.yaml; }
      { name = "minas-prowlarr.yaml";        path = ../minas-tirith/manifests/prowlarr.yaml; }
      { name = "minas-sonarr.yaml";          path = ../minas-tirith/manifests/sonarr.yaml; }
      { name = "minas-radarr.yaml";          path = ../minas-tirith/manifests/radarr.yaml; }
      { name = "minas-lidarr.yaml";          path = ../minas-tirith/manifests/lidarr.yaml; }
      { name = "minas-animearr.yaml";        path = ../minas-tirith/manifests/animearr.yaml; }
      { name = "minas-cleanuparr.yaml";      path = ../minas-tirith/manifests/cleanuparr.yaml; }
      { name = "minas-maintainerr.yaml";     path = ../minas-tirith/manifests/maintainerr.yaml; }
      { name = "minas-wrapperr.yaml";        path = ../minas-tirith/manifests/wrapperr.yaml; }
      { name = "minas-shelfmark.yaml";       path = ../minas-tirith/manifests/shelfmark.yaml; }
      # jellyfin is a StatefulSet, not a Deployment, because its database can only be
      # dumped while it is NOT running — see K3S-QUIESCE-DESIGN.md. The quiesce
      # ServiceAccount/Role/RoleBinding/CronJob that deletes `jellyfin-0` nightly is a
      # separate file so the RBAC is reviewable on its own.
      { name = "minas-jellyfin.yaml";        path = ../minas-tirith/manifests/jellyfin.yaml; }
      { name = "minas-jellyfin-quiesce.yaml"; path = ../minas-tirith/manifests/jellyfin-quiesce.yaml; }
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
      { name = "minas-plex.yaml";            path = ../minas-tirith/manifests/plex.yaml; }
      # readmeabook carries its OWN alias objects rather than adding them to
      # minas-docker-bridges.yaml: an ExternalName for `prowlarr` (which lives in the
      # `media` namespace) and a bridge for `gluetun`, which was selectorless while
      # gluetun was still a docker container and is now backed by the netns Pod.
      # Keeping them in this file means the workload and the names it depends on are
      # added and removed together, and it avoids a second AddOn owning objects in the
      # `books` namespace.
      { name = "minas-readmeabook.yaml";     path = ../minas-tirith/manifests/readmeabook.yaml; }
      # tracearr needs no alias objects: its only bare-name edge is tautulli, which is
      # already in `media`. Its SECRET is NOT delivered from here — it is rendered from
      # sops to tmpfs and applied by k3s-apply-secrets, because this directory is
      # unencrypted disk.
      { name = "minas-tracearr.yaml";        path = ../minas-tirith/manifests/tracearr.yaml; }
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
      { name = "minas-docker-bridges.yaml"; path = ../minas-tirith/manifests/docker-bridges.yaml; }
      # ---------------------------------------------------------------------------
      #  VPN-gated workloads — see minas-tirith/K3S-VPN-STACK-DESIGN.md
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
      { name = "minas-vpn-deluge-books.yaml"; path = ../minas-tirith/manifests/vpn-deluge-books.yaml; }
      # Same `minas-vpn-` prefix, same reason: it must sort AFTER
      # minas-docker-bridges.yaml, which owns the deluge-vpn Service and its
      # deluge-vpn-docker EndpointSlice until the cutover updates them in place.
      { name = "minas-vpn-deluge-vpn.yaml";   path = ../minas-tirith/manifests/vpn-deluge-vpn.yaml; }
      # The books netns trio as ONE Pod. Same `minas-vpn-` prefix and the same reason: it
      # must sort AFTER minas-readmeabook.yaml, which owns the `gluetun` Service and its
      # gluetun-docker EndpointSlice until the cutover updates them in place.
      { name = "minas-vpn-books-netns.yaml";  path = ../minas-tirith/manifests/books-netns.yaml; }
      # nextcloud: app + database as SEPARATE workloads. No bridge to take over (it was
      # never bridged — it is reached only through traefik), so no sort-order hazard here;
      # the `minas-nextcloud-` name is chosen for grouping, not ordering.
      { name = "minas-nextcloud.yaml";        path = ../minas-tirith/manifests/nextcloud.yaml; }
      # immich app + PostgreSQL + disposable Redis, all staged inert at replicas 0.
      { name = "minas-immich.yaml";           path = ../minas-tirith/manifests/immich.yaml; }
      # Permanently managed and inert while staged=false. The frozen basename and
      # zero-replica rendering make deactivation fail closed without deleting PVCs.
      { name = "minas-pin-collector.yaml"; path = pinCollectorManifest; }
      # This basename must remain distinct from k3s's packaged `traefik.yaml`.
      # The ingress replacement is staged inert; raising replicas is a gated cutover.
      { name = "minas-traefik.yaml";          path = ../minas-tirith/manifests/traefik.yaml; }
    ];
  };

  # Flattened, with the owning host carried through for the ownership map.
  manifestEntries = lib.concatLists (
    lib.mapAttrsToList (host: es: map (e: e // { owner = host; }) es) manifestsByHost
  );

  names = map (e: e.name) manifestEntries;

  # Files k3s writes into the auto-deploy directory ITSELF for its packaged
  # components. They are not ours, they reappear on every k3s start, and they must
  # NOT be reported as stale.
  #
  # This list exists because the stale check below originally omitted it and therefore
  # fired six warnings on EVERY activation. A warning that always fires is worse than
  # no warning: it teaches you to scroll past "stale k3s manifest", and the one time a
  # genuinely orphaned file appears you scroll past that too.
  #
  # Deliberately an explicit list rather than a wildcard. If a future k3s version adds
  # a packaged component, it warns exactly once — which is useful signal (a new
  # k3s-owned component appeared) rather than noise, and the fix is to add it here.
  k3sPackagedManifests = [
    "ccm.yaml"
    "coredns.yaml"
    "local-storage.yaml"
    "rolebindings.yaml"
    "runtimes.yaml"
    "traefik.yaml"
  ];

  # ---------------------------------------------------------------------------
  #  coredns-custom — resolve minas' public hostnames to its LAN address
  # ---------------------------------------------------------------------------
  # D13: a Pod resolving e.g. tautulli.saldivar.io gets the PUBLIC address, so its
  # request leaves the network and must return through NAT loopback. This maps those
  # names to 10.0.1.6 for cluster DNS, exactly as networking.hosts does for the host.
  #
  # The list is imported from minas-tirith/traefik-hostnames.nix — the SAME file
  # networking.hosts uses — so the two cannot drift.
  #
  # Delivered as a `.server` file, not `.override`, deliberately. k3s' Corefile carries
  # `import /etc/coredns/custom/*.override` INSIDE the .:53 block and
  # `import /etc/coredns/custom/*.server` at top level. The main block already uses the
  # `hosts` plugin for NodeHosts, and a second `hosts` in the same block is a config
  # error — so this defines its own server block for the zone instead.
  #
  # `fallthrough` matters: names NOT in the list (notably ha-pelargir.saldivar.io,
  # which lives on pelargir) fall through to `forward` and resolve normally.
  #
  # coredns-custom is the ONE CoreDNS customisation that is k3s-supported and
  # upgrade-safe. Both replicas mount it (packaged and coredns-ha), optional: true.
  minasHostnames = import ../minas-tirith/traefik-hostnames.nix;

  corednsCustom = pkgs.writeText "coredns-custom.yaml" ''
    # GENERATED by pelargir/manifests.nix from minas-tirith/traefik-hostnames.nix.
    apiVersion: v1
    kind: ConfigMap
    metadata:
      name: coredns-custom
      namespace: kube-system
    data:
      saldivar.server: |
        saldivar.io:53 {
            errors
            cache 30
            hosts {
    ${lib.concatMapStringsSep "\n" (h: "            10.0.1.6 ${h}") minasHostnames}
                fallthrough
            }
            forward . /etc/resolv.conf
        }
  '';

  plainManifests = pkgs.linkFarm "pelargir-k3s-manifests" manifestEntries;

  # ---------------------------------------------------------------------------
  #  A.1 — build-time validation + ownership map
  # ---------------------------------------------------------------------------
  # k3s auto-deploy has NO lifecycle: it applies what it finds and, critically,
  # **never deletes cluster objects when a manifest file disappears**. So a
  # malformed manifest is discovered only on the live cluster, and a manifest
  # dropped from the list above leaves BOTH a stale file here AND its objects
  # running with nothing reconciling them. A `nixos-rebuild --rollback` does not
  # undo either.
  #
  # This does two things at BUILD time, where failure is free:
  #   1. parses every manifest — a YAML error fails the build, not the cluster;
  #   2. emits an ownership map (file -> objects it declares) so that when a
  #      manifest is removed, there is a written list of what must be deleted by
  #      hand. Without it, "what did that file own?" is unanswerable after the
  #      fact, which is exactly when it is asked.
  ownershipMap = pkgs.runCommand "pelargir-k3s-ownership"
    { nativeBuildInputs = [ pkgs.yq-go ]; }
    ''
      echo "# k3s manifest ownership map — generated at build time" > $out
      echo "# k3s does NOT delete objects when a manifest is removed. If you drop a" >> $out
      echo "# manifest, delete the objects listed under it BY HAND." >> $out
      echo "" >> $out
      ${lib.concatMapStringsSep "\n" (e: ''
        echo "== [${e.owner}] ${e.name}" >> $out
        # `yq` exits non-zero on malformed YAML, failing the build here.
        yq -o=json '[.kind, (.metadata.namespace // "-"), .metadata.name] | @csv' \
          ${e.path} 2>/dev/null | tr -d '"' | sed 's/^/   /' >> $out \
          || { echo "MANIFEST PARSE FAILED: ${e.name}" >&2; exit 1; }
      '') manifestEntries}
    '';
in
assert lib.assertMsg (lib.length (lib.unique names) == lib.length names)
  ("pelargir/manifests.nix: duplicate manifest filenames — a later entry would "
   + "silently shadow an earlier one in the auto-deploy directory. Names: "
   + lib.concatStringsSep ", " names);
assert lib.assertMsg (!pinCollectorRelease.staged || pinCollectorRelease.registryPullSecretReady)
  "PinCollector cannot be staged before its SOPS-backed GHCR pull Secret is ready";
assert lib.assertMsg (
  !pinCollectorRelease.staged
  || (
    builtins.match "ghcr\\.io/edgarsaldivar/pin-collector-api@sha256:[0-9a-f]{64}" pinCollectorRelease.apiImage != null
    && builtins.match "ghcr\\.io/edgarsaldivar/pin-collector-model-service@sha256:[0-9a-f]{64}" pinCollectorRelease.modelImage != null
  )
) "A staged PinCollector release must use immutable GHCR sha256 references";
assert lib.assertMsg (!pinCollectorRelease.enabled || pinCollectorRelease.staged)
  "PinCollector cannot be enabled before its stateful restore stage";
{
  systemd.tmpfiles.rules = [
    "d /var/lib/rancher/k3s/server/manifests 0700 root root -"
  ];

  system.activationScripts.pelargir-k3s-manifests = lib.stringAfter [ "setupSecrets" ] ''
    install -d -m 0700 /var/lib/rancher/k3s/server/manifests
    for source in ${plainManifests}/*.yaml; do
      install -m 0600 "$source" "/var/lib/rancher/k3s/server/manifests/$(basename "$source")"
    done
    # The rendered Secret manifest is deliberately NOT installed here any more.
    #
    # It used to be copied into this directory, which is persistent disk on a host
    # with no full-disk encryption, leaving the mosquitto password, the Zigbee
    # network key and the Cloudflare API token readable in cleartext to anyone
    # holding the SD card or NVMe. Encrypting the datastore (P1B) did nothing for a
    # plaintext file sitting one directory above it.
    #
    # It is now applied straight from tmpfs by the k3s-apply-secrets unit
    # (pelargir/secrets.nix). Removing the stale copy is safe: k3s does not delete
    # objects when a manifest file disappears, so the Secrets stay in the cluster,
    # and that unit keeps them updated.
    #
    # ⛔ The k3s AddOn record `pelargir-home-secrets` still exists and now points at
    # this missing file. LEAVE IT. It is inert — k3s cannot reapply a manifest it
    # cannot read — but the Secret objects still carry its
    # `objectset.rio.cattle.io/owner-name` annotation, so deleting the AddOn may
    # garbage-collect the very Secrets this is protecting. Verified: a full k3s
    # restart does NOT recreate the file, and all 22 Secrets survive.
    rm -f /var/lib/rancher/k3s/server/manifests/pelargir-home-secrets.yaml

    # A.1 — ownership map, kept OUTSIDE the auto-deploy directory so k3s never
    # tries to apply it (auto-deploy only reads .yaml/.yml/.json).
    install -m 0600 ${ownershipMap} /var/lib/rancher/k3s/server/manifests-ownership.txt

    # A.1 — REPORT stale files; deliberately do NOT delete them.
    #
    # Removing a stale file stops it being reapplied but does NOT remove its
    # cluster objects — k3s has no pruning. Silently deleting files would
    # therefore produce orphaned objects with nothing reconciling them, which is
    # worse than leaving the file: at least the file records what exists. So this
    # warns, names the file, and points at the ownership map. Deletion stays a
    # deliberate human act until a real prune (object-level) is implemented.
    # pelargir-home-secrets.yaml is NOT listed: it is applied from tmpfs and must
    # never reappear here. Leaving it out means the stale check below WARNS if it
    # somehow comes back, which is exactly the alarm we want.
    ours="${lib.concatStringsSep " " names}"
    packaged="${lib.concatStringsSep " " k3sPackagedManifests}"
    for f in /var/lib/rancher/k3s/server/manifests/*.yaml; do
      [ -e "$f" ] || continue
      b=$(basename "$f")
      case " $ours $packaged " in
        *" $b "*) ;;
        *) echo "WARNING: stale k3s manifest $b is no longer declared in manifests.nix." >&2
           echo "         k3s will NOT delete its objects. See manifests-ownership.txt," >&2
           echo "         delete them by hand, then remove the file." >&2 ;;
      esac
    done
  '';
}
