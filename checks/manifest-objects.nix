# Validate the k3s manifest catalog as KUBERNETES, not merely as YAML.
#
# `pelargir/manifests.nix` already proves each file parses and asserts the
# installed basenames are unique. That is necessary and not sufficient: two
# DIFFERENT files can declare the SAME object, and nothing notices until the live
# API server arbitrates.
#
# Why that matters more here than in most clusters: k3s auto-deploy wraps each
# file in its own AddOn, and an AddOn records the objects it owns. Two AddOns
# claiming one object means whichever applies second takes ownership, the first
# keeps a stale claim, and deleting either can garbage-collect a live resource.
# `manifests.nix` says as much in its own header — this makes it checkable.
#
# Also catches the plainer mistake: a workload copied as a template for another,
# with the name changed in one place and not the other.
#
# ⛔ Scope, stated so the check is not trusted beyond it: this validates object
# IDENTITY, not schema. It does not know whether a Deployment's spec is valid, nor
# whether a CRD exists. A pinned kubeconform pass is tracked separately in ROADMAP;
# this is the half that needs no schema bundle and no network.
{
  lib,
  pkgs,
  ...
}:
let
  manifestDirs = [
    ../hosts/nixos/minas-tirith/manifests
    ../hosts/nixos/osgiliath/manifests
    ../hosts/nixos/pelargir/manifests
  ];

  yamlsIn =
    dir:
    let
      entries = builtins.readDir dir;
    in
    lib.mapAttrsToList (name: _: dir + "/${name}") (
      lib.filterAttrs (name: type: type == "regular" && lib.hasSuffix ".yaml" name) entries
    );

  manifests = lib.flatten (map yamlsIn manifestDirs);
  args = lib.concatMapStringsSep " " (m: "${m}") manifests;
in
pkgs.runCommand "manifest-objects"
  {
    nativeBuildInputs = [
      pkgs.yq-go
      pkgs.coreutils
    ];
  }
  ''
        set -euo pipefail
        : > ids

        # Map each store path back to its human name. `basename` on a store path keeps
        # the hash prefix, which makes a 2am failure message harder to read than it
        # needs to be.
        declare -A NAMES
    ${lib.concatMapStringsSep "\n" (
      m: "    NAMES[${m}]=\"${lib.last (lib.splitString "/" (toString m))}\""
    ) manifests}

        for f in ${args}; do
          # Every document in the file. `// ""` keeps a missing namespace from
          # collapsing the tuple and silently colliding with a cluster-scoped object.
          yq -N '[.apiVersion // "-", .kind // "-", .metadata.namespace // "cluster", .metadata.name // "-"] | join("|")' \
            "$f" 2>/dev/null \
            | grep -v '^---$' | grep -v '^$' | grep -v '^-|-|' \
            | sed "s|$| <- ''${NAMES[$f]}|" >> ids \
            || { echo "MANIFEST PARSE FAILED: $f" >&2; exit 1; }
        done

        # ⛔ Refuse to pass vacuously. If yq stops producing tuples — a version bump, a
        # changed expression — every assertion below is trivially satisfied and this
        # check would report success having compared nothing.
        count=$(wc -l < ids | tr -d ' ')
        if [ "$count" -lt 100 ]; then
          echo "manifest-objects extracted only $count objects from ${toString (builtins.length manifests)} files;" >&2
          echo "extraction is broken and this check would pass vacuously" >&2
          exit 1
        fi

        # An object identity claimed by more than one FILE.
        if cut -d' ' -f1 ids | sort | uniq -d > dupes && [ -s dupes ]; then
          echo "The same Kubernetes object is declared in more than one manifest:" >&2
          while read -r id; do
            echo "  $id" >&2
            grep -F "$id <- " ids | sed 's/^/      /' >&2
          done < dupes
          echo "" >&2
          echo "k3s wraps each file in its own AddOn. Two AddOns owning one object means" >&2
          echo "the later apply takes ownership, the earlier keeps a stale claim, and" >&2
          echo "deleting either can garbage-collect a live resource." >&2
          exit 1
        fi

        echo "$count objects across ${toString (builtins.length manifests)} manifests, all identities unique."
        touch $out
  ''
