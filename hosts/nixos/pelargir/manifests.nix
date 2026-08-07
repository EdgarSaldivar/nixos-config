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
      { name = "minas-audiobookshelf.yaml"; path = ../minas-tirith/manifests/audiobookshelf.yaml; }
      { name = "minas-nvidia-device-plugin.yaml"; path = ../minas-tirith/manifests/nvidia-device-plugin.yaml; }
      { name = "minas-komga.yaml";          path = ../minas-tirith/manifests/komga.yaml; }
      { name = "minas-palworld.yaml";       path = ../minas-tirith/manifests/palworld.yaml; }
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
