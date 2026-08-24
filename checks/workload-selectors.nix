# A workload's selector must match the Pod template it selects.
#
# `spec.selector.matchLabels` must be a SUBSET of `spec.template.metadata.labels`.
# If it is not, the API server rejects the object outright — and because these
# manifests are delivered by k3s auto-deploy rather than applied interactively, the
# rejection surfaces as an AddOn that quietly never converges rather than as an
# error somebody sees.
#
# ⛔ Worse than a plain rejection: `spec.selector` is IMMUTABLE after creation. A
# workload created with the wrong selector cannot be corrected by editing the
# manifest — it needs a delete-and-recreate, on a live hostPath workload, which is
# exactly the class of operation this repository spends so much effort avoiding.
#
# This is the schema-adjacent defect worth checking directly. Full `kubeconform`
# validation was considered and rejected for now: it needs a multi-hundred-megabyte
# schema repository vendored into the store to run offline, which is a poor trade
# for a five-host fleet. Live schema validation belongs to k3s reconciliation.
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
    lib.mapAttrsToList (name: _: dir + "/${name}") (
      lib.filterAttrs (name: type: type == "regular" && lib.hasSuffix ".yaml" name) (builtins.readDir dir)
    );

  manifests = lib.flatten (map yamlsIn manifestDirs);
  args = lib.concatMapStringsSep " " (m: "${m}") manifests;
  names = lib.concatMapStringsSep "\n" (
    m: "    NAMES[${m}]=\"${lib.last (lib.splitString "/" (toString m))}\""
  ) manifests;
in
pkgs.runCommand "workload-selectors"
  {
    nativeBuildInputs = [
      pkgs.yq-go
      pkgs.coreutils
    ];
  }
  ''
        set -euo pipefail
        declare -A NAMES
    ${names}

        : > report
        for f in ${args}; do
          # kind|namespace|name|selector-labels|template-labels, one line per workload.
          yq -N '
            select(.kind == "Deployment" or .kind == "StatefulSet" or .kind == "DaemonSet")
            | .kind + "|" + (.metadata.namespace // "-") + "|" + .metadata.name
              + "|" + (.spec.selector.matchLabels // {} | to_entries | map(.key + "=" + .value) | sort | join(","))
              + "|" + (.spec.template.metadata.labels // {} | to_entries | map(.key + "=" + .value) | sort | join(","))
          ' "$f" > raw 2> err || {
            # ⛔ A per-file yq FAILURE must not be swallowed.
            #
            # This was `2>/dev/null ... || true`, which discarded the error and let the
            # AGGREGATE floor below decide. That floor cannot see a per-file failure: a
            # yq version bump or a type error on ONE manifest silently contributes zero
            # workloads while the other 30+ still clear the floor, so the check passes
            # while blind to that file -- the exact vacuity the floor was added to stop,
            # just at a granularity the floor cannot resolve.
            echo "workload-selectors: yq FAILED on $f" >&2
            cat err >&2
            exit 1
          }

          # ⚠️ Only grep's "no lines matched" is tolerated, and ONLY that.
          #
          # `... | sed ... >> report || true` was wrong: `|| true` covers the WHOLE
          # pipeline, so a grep I/O error or a sed failure was swallowed just as
          # silently as the benign case, and the aggregate floor cannot see one file
          # contributing nothing. grep distinguishes them by exit code -- 0 matched,
          # 1 matched nothing, >1 actual error -- so branch on that and let `set -e`
          # kill the build if `sed` itself fails.
          #
          # Exit 1 is the correct, expected outcome for a manifest declaring no
          # Deployment/StatefulSet/DaemonSet at all (a Middleware- or Secret-only file).
          rc=0
          grep -v '^$' raw > filtered || rc=$?
          if [ "$rc" -gt 1 ]; then
            echo "workload-selectors: grep FAILED (exit $rc) on $f" >&2
            exit 1
          fi
          sed "s|$| <- ''${NAMES[$f]}|" filtered >> report
        done

        # ⛔ Vacuity guard. If the yq expression stops matching — a version bump, a
        # schema change — every comparison below is trivially satisfied and this check
        # would report success having examined nothing.
        count=$(wc -l < report | tr -d ' ')
        if [ "$count" -lt 30 ]; then
          echo "workload-selectors found only $count workloads across ${toString (builtins.length manifests)} manifests;" >&2
          echo "extraction is broken and this check would pass vacuously" >&2
          exit 1
        fi

        bad=0
        while IFS='|' read -r kind ns name sel rest; do
          tpl="''${rest%% <- *}"
          src="''${rest##* <- }"
          # Every selector label must appear verbatim in the template labels.
          IFS=',' read -ra pairs <<< "$sel"
          for p in "''${pairs[@]}"; do
            [ -n "$p" ] || continue
            case ",$tpl," in
              *",$p,"*) ;;
              *)
                echo "SELECTOR MISMATCH  $kind $ns/$name  ($src)" >&2
                echo "    selector wants: $p" >&2
                echo "    template has:   $tpl" >&2
                bad=1
                ;;
            esac
          done
          # A selector with no labels selects everything in the namespace.
          if [ -z "$sel" ]; then
            echo "EMPTY SELECTOR  $kind $ns/$name ($src) selects every Pod in its namespace" >&2
            bad=1
          fi
        done < report

        if [ "$bad" -ne 0 ]; then
          echo "" >&2
          echo "spec.selector is IMMUTABLE after creation. A workload created with the" >&2
          echo "wrong selector cannot be fixed by editing the manifest -- it needs a" >&2
          echo "delete-and-recreate on live state." >&2
          exit 1
        fi

        echo "$count workloads checked; every selector matches its Pod template."
        touch $out
  ''
