{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:

# Dungeon Scriber's deployment invariants, checked on the RENDERED manifest (both the
# declared release and a synthetic staged/enabled/exposed one, through the production
# renderer) and on the host pieces that must agree with it:
#
#   * every workload and the blob volume are pinned to the configured node, and the
#     template names no node itself;
#   * every image is digest-pinned, no Secret object or secretKeyRef env is declared,
#     nothing is published through a hostPort, Ingress or LoadBalancer;
#   * PostgreSQL sits on local-path-retain and initialises into a subdirectory;
#   * the inert render runs nothing, and the exposed render is a Local NodePort;
#   * minas drops that NodePort on every interface but the tailnet, and the optional
#     tailscale serve unit is off by default, fronts it on loopback, closes the direct
#     path when on, and is refused without exposure and one trusted proxy hop;
#   * the backup program never copies this namespace in plaintext and mirrors the
#     configured blob root.
#
# The manifest-objects and workload-selectors checks only read literal `.yaml` files,
# so the selector rule is repeated here for this templated manifest.
let
  release = import ../hosts/nixos/minas-tirith/dungeon-scriber-release.nix;
  contract = import ../hosts/nixos/minas-tirith/dungeon-scriber-release-contract.nix { inherit lib; };
  render = import ../hosts/nixos/pelargir/dungeon-scriber-manifest.nix { inherit lib pkgs; };
  sha = lib.concatStrings (lib.replicate 40 "a");
  live = contract.assertValid (
    release
    // {
      staged = true;
      enabled = true;
      runtimeSecretReady = true;
      registryPullSecretReady = true;
      tailnetExposure = true;
      gitRevision = sha;
      apiImageRevision = sha;
      apiImage = "ghcr.io/edgarsaldivar/dungeon-scriber-api@sha256:${lib.concatStrings (lib.replicate 64 "1")}";
    }
  );
  declared = render (contract.assertValid release);
  exposed = render live;

  template = builtins.readFile ../hosts/nixos/minas-tirith/manifests/dungeon-scriber.yaml.in;
  backup = builtins.readFile ../hosts/nixos/minas-tirith/scripts/backup-root-data.sh;
  minas = nixosConfigurations.minas-tirith;
  firewall = minas.config.networking.firewall.extraCommands;
  # Option 2: the same host with tailscale serve switched on. Only its unit, firewall
  # text and assertions are evaluated, never the toplevel.
  served =
    (minas.extendModules { modules = [ { minas.dungeonScriber.tailnetServe.enable = true; } ]; }).config;
  serveUnit = served.systemd.services.dungeon-scriber-tailnet-serve;
  port = toString release.tailnet.port;

  hostProblems =
    lib.optional (
      lib.hasInfix "kubernetes.io/hostname: minas" template || lib.hasInfix "values: [minas" template
    ) "the manifest template names a node literally instead of @nodeName@"
    ++ lib.optional (!lib.hasInfix "{ name: PGDATA, value: /var/lib/postgresql/data/pgdata }" template)
      "PostgreSQL must initialise into a subdirectory of its volume mount"
    ++
      lib.optional
        (!lib.hasInfix "-t raw -A PREROUTING -p tcp --dport ${port} ! -i ${release.tailnet.interface} -j DROP" firewall)
        "minas does not drop the tailnet NodePort on non-tailnet interfaces"
    ++ lib.optional (minas.config.systemd.services ? dungeon-scriber-tailnet-serve)
      "tailscale serve is on by default; it must stay opt-in"
    ++ lib.optional (!lib.hasInfix "tailscale serve --bg --https=443 http://127.0.0.1:${port}" serveUnit.script)
      "the serve unit does not proxy tailnet HTTPS 443 to the loopback NodePort"
    ++ lib.optional (!lib.hasInfix "tailscale serve --https=443 off" serveUnit.preStop)
      "disabling serve would leave its handler in tailscaled's persistent state"
    ++ lib.optional (!lib.hasInfix "-t raw -A PREROUTING -p tcp --dport ${port} ! -i lo -j DROP" served.networking.firewall.extraCommands)
      "with serve on, the NodePort must be closed to everything but loopback"
    ++ lib.optional (!lib.hasInfix "-t raw -D PREROUTING -p tcp --dport ${port} ! -i ${release.tailnet.interface} -j DROP" served.networking.firewall.extraCommands)
      "toggling serve would leave the previous raw-table rule behind"
    ++ lib.optional (
      (!release.tailnetExposure || release.api.trustProxyHops != 1)
      && lib.all (a: a.assertion) served.assertions
    ) "serve can be enabled without tailnetExposure and trustProxyHops = 1"
    ++ lib.optional (!lib.hasInfix "ds_blob_root=${release.storage.blobHostPath}\n" backup)
      "the backup program's blob root differs from the release's blobHostPath"
    ++ lib.optional (!lib.hasInfix "--exclude='pvc-*_dungeon-scriber_*/***'" backup)
      "the backup rsync would copy Dungeon Scriber's PVCs in plaintext"
    ++ lib.optional (!lib.hasInfix ''|| [ "$kns" = dungeon-scriber ]; then'' backup)
      "Dungeon Scriber's database dump is not forced into the age-encrypted branch";
in
if hostProblems != [ ] then
  throw "Dungeon Scriber deployment contract: ${lib.concatStringsSep "; " hostProblems}"
else
  pkgs.runCommand "dungeon-scriber-deployment-contract"
    {
      nativeBuildInputs = [ pkgs.yq-go ];
      node = release.placement.nodeName;
      inherit port;
      pgClass = release.storage.postgresStorageClass;
    }
    ''
      set -euo pipefail
      fail() { echo "dungeon-scriber-deployment-contract: $*" >&2; exit 1; }

      for f in ${declared} ${exposed}; do
        count=$(yq -N '.kind' "$f" | grep -c . || true)
        [ "$count" -ge 10 ] || fail "only $count objects parsed from $f; extraction is broken"

        [ -z "$(yq -N 'select(.kind == "Secret" or .kind == "Ingress" or .kind == "IngressRoute") | .kind' "$f")" ] \
          || fail "a Secret or ingress object is declared in the public manifest"
        [ -z "$(yq -N 'select(.spec.type == "LoadBalancer") | .metadata.name' "$f")" ] || fail "LoadBalancer Service"
        [ -z "$(yq -N '[.. | select((tag == "!!map") and has("secretKeyRef"))] | length' "$f" | grep -vx 0)" ] \
          || fail "secretKeyRef env persists secrets in containerd metadata"
        [ -z "$(yq -N '[.. | select((tag == "!!map") and has("hostPort"))] | length' "$f" | grep -vx 0)" ] \
          || fail "hostPort declared; exposure is the tailnet NodePort only"

        # Every Pod template is pinned to the configured node.
        yq -N 'select(.spec.template) | .metadata.name + "=" + (.spec.template.spec.nodeSelector."kubernetes.io/hostname" // "")' "$f" \
          | while IFS== read -r name value; do
              [ "$value" = "$node" ] || fail "$name is pinned to '$value', not $node"
            done
        [ "$(yq -N 'select(.kind == "PersistentVolume") | .spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]' "$f")" = "$node" ] \
          || fail "the blob PersistentVolume is not pinned to $node"
        [ "$(yq -N 'select(.kind == "PersistentVolume") | .spec.persistentVolumeReclaimPolicy' "$f")" = Retain ] \
          || fail "the blob PersistentVolume must be Retain"

        # Every image is immutable.
        yq -N '.. | select((tag == "!!map") and has("image")) | .image' "$f" | while read -r image; do
          case "$image" in *@sha256:*) ;; *) fail "mutable image reference $image" ;; esac
        done

        [ "$(yq -N 'select(.kind == "PersistentVolumeClaim" and .metadata.name == "postgres-data") | .spec.storageClassName' "$f")" = "$pgClass" ] \
          || fail "postgres-data is not on $pgClass"

        # Selectors must be a subset of the Pod template labels (they are immutable).
        yq -N 'select(.spec.selector.matchLabels) | .metadata.name + " " + (.spec.selector.matchLabels | to_entries | map(.key + "=" + .value) | join(",")) + " " + (.spec.template.metadata.labels | to_entries | map(.key + "=" + .value) | join(","))' "$f" \
          | while read -r name sel labels; do
              IFS=, read -ra pairs <<< "$sel"
              for p in "''${pairs[@]}"; do
                case ",$labels," in *",$p,"*) ;; *) fail "$name selector $p is not in its template labels" ;; esac
              done
            done
      done

      # The declared release is inert until the gates are raised.
      if [ "${lib.boolToString release.staged}" = false ]; then
        [ -z "$(yq -N 'select(.spec.replicas != null and .spec.replicas != 0) | .metadata.name' ${declared})" ] \
          || fail "an unstaged release renders non-zero replicas"
        [ "$(yq -N 'select(.kind == "Job") | .spec.suspend' ${declared})" = true ] || fail "unstaged migration Job not suspended"
      fi
      if [ "${lib.boolToString release.tailnetExposure}" = false ]; then
        [ "$(yq -N 'select(.metadata.name == "api-tailnet") | .spec.type' ${declared})" = ClusterIP ] \
          || fail "api-tailnet is exposed while tailnetExposure is false"
      fi

      # The exposed shape: one API, a running migration, a Local NodePort on the gated port.
      [ "$(yq -N 'select(.kind == "Deployment") | .spec.replicas' ${exposed})" = 1 ] || fail "enabled API is not one replica"
      [ "$(yq -N 'select(.kind == "Job") | .spec.suspend' ${exposed})" = false ] || fail "enabled migration Job suspended"
      [ "$(yq -N 'select(.metadata.name == "api-tailnet") | .spec.type' ${exposed})" = NodePort ] || fail "no tailnet NodePort"
      [ "$(yq -N 'select(.metadata.name == "api-tailnet") | .spec.externalTrafficPolicy' ${exposed})" = Local ] \
        || fail "tailnet NodePort must be externalTrafficPolicy Local"
      [ "$(yq -N 'select(.metadata.name == "api-tailnet") | .spec.ports[0].nodePort' ${exposed})" = "$port" ] \
        || fail "tailnet NodePort differs from the gated port $port"

      # A new API waits for the Job's schema through the read-only check, never migrating itself.
      [ "$(yq -N 'select(.kind == "Deployment") | .spec.template.spec.initContainers[0].name' ${exposed})" = require-current-schema ] \
        || fail "the API does not wait for a current schema"
      yq -N 'select(.kind == "Deployment") | .spec.template.spec.initContainers[0].args[0]' ${exposed} \
        | grep -q 'migrate.js --check' || fail "the API schema gate must be migrate.js --check"

      echo "Dungeon Scriber manifests pinned to $node, digest-pinned, tailnet-gated on $port."
      touch $out
    ''
