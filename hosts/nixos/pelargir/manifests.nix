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
  rendered = import ./manifest-renderers.nix { inherit lib pkgs; };
  inherit (rendered) pinCollectorRelease;

  # Everything is delivered from pelargir's single auto-deploy directory — agents
  # have none — so this is organisational separation, not blast-radius isolation.
  # A malformed manifest for any owner still reaches the cluster through this one
  # server, and a Nix activation reapplies every file.
  #
  # ⚠️ FILENAMES ARE FROZEN. Renaming an entry creates a new auto-deploy file and
  # leaves the old one behind forever, still being applied, because k3s never prunes.
  # Keep this explicit owner order aligned with the former manifestsByHost attrset's
  # lexicographic mapAttrsToList order.
  manifestCatalogs = [
    {
      owner = "cluster";
      entries = import ./manifest-catalog/cluster.nix {
        inherit (rendered) corednsCustom;
      };
    }
    {
      owner = "minas-tirith";
      entries = import ./manifest-catalog/minas-tirith.nix {
        inherit (rendered) minasTraefik pinCollectorManifest;
      };
    }
    {
      owner = "osgiliath";
      entries = import ./manifest-catalog/osgiliath.nix;
    }
    {
      owner = "pelargir";
      entries = import ./manifest-catalog/pelargir.nix;
    }
  ];

  # Flattened, with the owning host carried through for the ownership map.
  manifestEntries = lib.concatMap (
    catalog: map (entry: entry // { inherit (catalog) owner; }) catalog.entries
  ) manifestCatalogs;

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
  ownershipMap = pkgs.runCommand "pelargir-k3s-ownership" { nativeBuildInputs = [ pkgs.yq-go ]; } ''
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
assert lib.assertMsg (lib.length (lib.unique names) == lib.length names) (
  "pelargir/manifests.nix: duplicate manifest filenames — a later entry would "
  + "silently shadow an earlier one in the auto-deploy directory. Names: "
  + lib.concatStringsSep ", " names
);
assert lib.assertMsg (
  !pinCollectorRelease.staged || pinCollectorRelease.registryPullSecretReady
) "PinCollector cannot be staged before its SOPS-backed GHCR pull Secret is ready";
assert lib.assertMsg (
  !pinCollectorRelease.staged
  || (
    builtins.match "ghcr\\.io/edgarsaldivar/pin-collector-api@sha256:[0-9a-f]{64}" pinCollectorRelease.apiImage
    != null
    &&
      builtins.match "ghcr\\.io/edgarsaldivar/pin-collector-model-service@sha256:[0-9a-f]{64}" pinCollectorRelease.modelImage
      != null
  )
) "A staged PinCollector release must use immutable GHCR sha256 references";
assert lib.assertMsg (
  !pinCollectorRelease.enabled || pinCollectorRelease.staged
) "PinCollector cannot be enabled before its stateful restore stage";
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
    # The former `pelargir-home-secrets` AddOn was removed only after its Secret
    # objects were disowned and proven reproducible through k3s-apply-secrets. If a
    # datastore restore makes that AddOn reappear, k3s-reconcile reports its missing
    # source as a gap. Inspect and disown anything it owns before deleting it again;
    # deleting an owning AddOn can garbage-collect live credentials.
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
