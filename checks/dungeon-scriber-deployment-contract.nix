{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:

# Dungeon Scriber's deployment invariants. Gate behaviour is tested on SYNTHETIC
# releases (all off; exposed over plain tailnet HTTP; exposed through Serve), rendered
# by the production manifest renderer and fed to minas' production module, so the
# result never depends on which gates the deployed release file happens to raise.
# The deployed release is checked only for what must hold in every state.
#
#   * every workload and the blob volume are pinned to the configured node, and the
#     template names no node itself;
#   * every image is digest-pinned; no Secret object, secretKeyRef env, hostPort,
#     Ingress or LoadBalancer is declared;
#   * PostgreSQL sits on local-path-retain and initialises into a subdirectory;
#   * the API refuses an unmounted blob dataset and an old schema, rolls on any
#     settings change, and admits direct tailnet clients only while that is the
#     intended path;
#   * minas drops the NodePort, for its own addresses only, on every interface but the
#     tailnet, or on every interface but loopback while Serve fronts it;
#   * Serve is refused unless the release, trusted hops and exposure agree;
#   * the backup program never copies this namespace in plaintext and mirrors the
#     configured, mounted blob dataset.
#
# The manifest-objects and workload-selectors checks only read literal `.yaml` files,
# so the selector rule is repeated here for this templated manifest.
let
  release = import ../hosts/nixos/minas-tirith/dungeon-scriber-release.nix;
  contract = import ../hosts/nixos/minas-tirith/dungeon-scriber-release-contract.nix { inherit lib; };
  render =
    r:
    import ../hosts/nixos/pelargir/dungeon-scriber-manifest.nix { inherit lib pkgs; } (
      contract.assertValid r
    );

  sha = lib.concatStrings (lib.replicate 40 "a");
  fixtureNode = "fixture-node";
  fixturePort = 30080;
  off = {
    staged = false;
    enabled = false;
    runtimeSecretReady = false;
    registryPullSecretReady = false;
    tailnetExposure = false;
    gitRevision = null;
    apiImage = null;
    apiImageRevision = null;
    placement.nodeName = fixtureNode;
    storage = {
      postgresStorageClass = "local-path-retain";
      postgresSize = "1Gi";
      blobDataset = "storage/fixture/blobs";
      blobHostPath = "/storage/fixture/blobs";
      blobCapacity = "1Gi";
    };
    tailnet = {
      port = fixturePort;
      interface = "tailscale0";
      clientCidr = "100.64.0.0/10";
      https = false;
    };
    public = {
      enable = false;
      hostname = "fixture.saldivar.io";
      ingressNamespace = "fixture-ingress";
      ingressApp = "fixture-proxy";
    };
    api = {
      trustProxyHops = 0;
      logLevel = "info";
      defaultEntitlements = "beta-all";
      loginRateLimitMax = 60;
      rotationRateLimitMax = 600;
    };
  };
  direct = off // {
    staged = true;
    enabled = true;
    runtimeSecretReady = true;
    registryPullSecretReady = true;
    tailnetExposure = true;
    gitRevision = sha;
    apiImageRevision = sha;
    apiImage = "ghcr.io/edgarsaldivar/dungeon-scriber-api@sha256:${lib.concatStrings (lib.replicate 64 "1")}";
  };
  https = direct // {
    tailnet = direct.tailnet // {
      https = true;
    };
    api = direct.api // {
      trustProxyHops = 1;
    };
  };

  # Serve still on, API already at 0 hops: the state the API rolls into before the
  # policy opens when Serve is turned off (and out of before it trusts the hop).
  draining = https // {
    api = https.api // {
      trustProxyHops = 0;
    };
  };

  # The public route in front of Serve (the deployed shape), and on its own.
  public = https // {
    public = https.public // {
      enable = true;
    };
  };
  publicOnly = public // {
    tailnetExposure = false;
    tailnet = public.tailnet // {
      https = false;
    };
  };

  renders = {
    declared = render release;
    public = render public;
    publicOnly = render publicOnly;
    off = render off;
    direct = render direct;
    https = render https;
    draining = render draining;
  };

  template = builtins.readFile ../hosts/nixos/minas-tirith/manifests/dungeon-scriber.yaml.in;
  backup = builtins.readFile ../hosts/nixos/minas-tirith/scripts/backup-root-data.sh;
  minas = nixosConfigurations.minas-tirith;
  # Only firewall text, units and assertions are evaluated, never a toplevel.
  host = modules: (minas.extendModules { inherit modules; }).config;
  hostFor = r: host [ { minas.dungeonScriber.release = r; } ];
  dsAssertions =
    cfg: lib.filter (a: !a.assertion && lib.hasInfix "minas.dungeonScriber" a.message) cfg.assertions;

  ruleFor =
    op: r: via:
    "-t raw ${op} PREROUTING -p tcp --dport ${toString r.tailnet.port} -m addrtype --dst-type LOCAL ! -i ${via} -j DROP";
  fw = cfg: cfg.networking.firewall.extraCommands;

  hostOff = hostFor off;
  hostDirect = hostFor direct;
  hostHttps = hostFor https;
  serveUnit = hostHttps.systemd.services.dungeon-scriber-tailnet-serve;

  # The public route, through the production catalog.
  catalogFor =
    r:
    import ../hosts/nixos/minas-tirith/traefik-routes/catalog.nix {
      pinCollectorRelease = import ../hosts/nixos/minas-tirith/pin-collector-release.nix;
      dungeonScriberRelease = r;
    };
  publicRoute = (catalogFor public).routes.dungeon-scriber;
  deployedCatalog = catalogFor release;
  publicRouteFile =
    (lib.findFirst (e: e.name == "dungeon-scriber") null
      (import ../hosts/nixos/minas-tirith/traefik-routes/render.nix {
        inherit lib pkgs;
        inherit (catalogFor public) authentikRollout legacyBasicAuthFallbackRoutes routes;
      }).rendered
    ).file;
  # The API trusts one forwarded hop, so Traefik must never pass on a client's own
  # X-Forwarded-For: forwarded headers are trusted only from the Cloudflare ranges
  # substituted into the https entrypoint, never `insecure`.
  traefikManifest = builtins.readFile ../hosts/nixos/minas-tirith/manifests/traefik.yaml;
  forwardedArgs = lib.filter (
    l: lib.hasInfix "forwardedHeaders" l && !lib.hasPrefix "#" (lib.trim l)
  ) (lib.splitString "\n" traefikManifest);

  problems =
    lib.optional (
      lib.hasInfix "kubernetes.io/hostname: minas" template || lib.hasInfix "values: [minas" template
    ) "the manifest template names a node literally instead of @nodeName@"
    ++ lib.optional (
      !lib.hasInfix "{ name: PGDATA, value: /var/lib/postgresql/data/pgdata }" template
    ) "PostgreSQL must initialise into a subdirectory of its volume mount"

    # The deployed host agrees with the deployed release, whatever its gates.
    ++ lib.optional (
      !lib.hasInfix (ruleFor "-A" release (
        if release.tailnet.https then "lo" else release.tailnet.interface
      )) (fw minas.config)
    ) "minas' NodePort rule does not match the deployed release"
    ++ lib.optional (
      (minas.config.systemd.services ? dungeon-scriber-tailnet-serve) != release.tailnet.https
    ) "minas must run Serve exactly when the deployed release asks for tailnet HTTPS"

    # All off and direct exposure: tailnet-only rule, no Serve, no failing assertion.
    ++ lib.optional (
      !lib.hasInfix (ruleFor "-A" off "tailscale0") (fw hostOff)
    ) "the all-off host lacks the tailnet-only NodePort rule"
    ++ lib.optional (
      hostOff.systemd.services ? dungeon-scriber-tailnet-serve
    ) "Serve runs with every gate off"
    ++ lib.optional (dsAssertions hostOff != [ ]) "the all-off release trips a host assertion"
    ++ lib.optional (
      !lib.hasInfix (ruleFor "-A" direct "tailscale0") (fw hostDirect)
    ) "the exposed host lacks the tailnet-only NodePort rule"
    ++ lib.optional (
      dsAssertions hostDirect != [ ]
    ) "the direct-exposure release trips a host assertion"

    # Serve: loopback-only rule, the previous rule removed, a unit that proxies and cleans up.
    ++ lib.optional (
      !lib.hasInfix (ruleFor "-A" https "lo") (fw hostHttps)
    ) "with Serve on, the NodePort must be closed to everything but loopback"
    ++ lib.optional (
      !lib.hasInfix (ruleFor "-D" https "tailscale0") (fw hostHttps)
    ) "toggling Serve would leave the tailnet-only rule behind"
    ++ lib.optional (
      !lib.hasInfix "tailscale serve --bg --https=443 http://127.0.0.1:${toString fixturePort}" serveUnit.script
    ) "the Serve unit does not proxy tailnet HTTPS 443 to the loopback NodePort"
    ++ lib.optional (
      !lib.hasInfix "tailscale serve --https=443 off" serveUnit.preStop
    ) "disabling Serve would leave its handler in tailscaled's persistent state"
    ++ lib.optional (dsAssertions hostHttps != [ ]) "the Serve release trips a host assertion"

    # Serve is refused when the host and the release disagree, or the hops are wrong.
    ++ lib.optional (
      dsAssertions (host [
        {
          minas.dungeonScriber.release = direct;
          minas.dungeonScriber.tailnetServe.enable = true;
        }
      ]) == [ ]
    ) "Serve can be switched on while the release still renders a direct-tailnet API"
    ++ lib.optional (
      dsAssertions (
        hostFor (
          https
          // {
            api = https.api // {
              trustProxyHops = 2;
            };
          }
        )
      ) == [ ]
    ) "Serve can run while the API trusts more than Serve's one hop"
    ++ lib.optional (
      dsAssertions (hostFor draining) != [ ]
    ) "the transitional Serve-with-0-hops release trips a host assertion"
    ++ lib.optional (
      dsAssertions (
        hostFor (
          off
          // {
            tailnet = off.tailnet // {
              https = true;
            };
          }
        )
      ) == [ ]
    ) "Serve can run without tailnet exposure"

    # The public route follows public.enable, targets the API Service, and is never
    # behind Authentik (the app has its own login).
    ++ lib.optional ((catalogFor https).routes.dungeon-scriber.enabled) "the public route is published while public.enable is off"
    ++ lib.optional (!publicRoute.enabled) "the public route is not published with public.enable on"
    ++ lib.optional (
      publicRoute.hosts != [ public.public.hostname ]
      || publicRoute.namespace != "dungeon-scriber"
      || publicRoute.serviceName != "api"
      || publicRoute.port != 3001
    ) "the public route does not target dungeon-scriber/api:3001 for the configured hostname"
    # An allowlist of the public API only: never /internal (workers use Serve) or
    # /ready. checks/dungeon-scriber-edge-contract.nix proves the behaviour on Traefik.
    ++ lib.optional (
      publicRoute.allowedPathPrefixes or [ ] != [ "/v1/" ]
      || publicRoute.allowedPaths or [ ] != [ "/health" ]
      || publicRoute.rejectedPathPatterns or [ ] == [ ]
    ) "the public route is not the /v1/ + /health allowlist with dot-segment rejection"
    # And the explicit denials, independent of the allowlist.
    ++ lib.optional (
      !lib.elem "/internal" (publicRoute.excludedPathPrefixes or [ ])
    ) "the public route does not explicitly deny /internal (worker routes are tailnet-only)"
    ++ lib.optional (
      !lib.elem "/ready" (publicRoute.excludedPaths or [ ])
    ) "the public route does not explicitly deny /ready"
    # Only the edge-headers middleware: no Authentik (the app has its own login) and no
    # buffering (uploads stream).
    ++ lib.optional (
      lib.elem "dungeon-scriber" deployedCatalog.authentikRollout.protectedRoutes
      || lib.elem "dungeon-scriber" deployedCatalog.authentikCandidateRoutes
      || lib.elem "dungeon-scriber" deployedCatalog.legacyBasicAuthFallbackRoutes
      || (publicRoute.middlewares or [ ]) != [ "k8s-dungeon-scriber-headers@file" ]
      || lib.attrNames (publicRoute.dynamic.middlewares or { }) != [ "k8s-dungeon-scriber-headers" ]
      || lib.attrNames publicRoute.dynamic.middlewares.k8s-dungeon-scriber-headers != [ "headers" ]
    ) "the public route's middlewares must be exactly its own headers middleware"
    ++ lib.optional (
      map lib.trim forwardedArgs
      != [ "- --entrypoints.https.forwardedHeaders.trustedIPs=@cloudflareTrustedIPsV4@" ]
    ) "Traefik's forwarded-header trust changed; the API's one trusted hop depends on it"
    ++ lib.optional (
      publicRoute.serversTransport or null != "k8s-dungeon-scriber@file"
      || !(publicRoute.dynamic.serversTransports ? k8s-dungeon-scriber)
    ) "the public route must use its own bounded serversTransport"
    ++ lib.optional (
      deployedCatalog.routes.dungeon-scriber.enabled != release.public.enable
    ) "the deployed route disagrees with the deployed release"

    # The backup program agrees with the deployed release.
    ++ lib.optional (
      !lib.hasInfix "ds_blob_root=${release.storage.blobHostPath}\n" backup
    ) "the backup program's blob root differs from the release's blobHostPath"
    ++ lib.optional (
      !lib.hasInfix "ds_blob_dataset=${release.storage.blobDataset}\n" backup
    ) "the backup program's blob dataset differs from the release's blobDataset"
    ++ lib.optional (
      !lib.hasInfix ''findmnt -no SOURCE --mountpoint "$root"'' backup
    ) "the backup does not require the blob dataset to be mounted"
    ++ lib.optional (
      !lib.hasInfix "--exclude='pvc-*_dungeon-scriber_*/***'" backup
    ) "the backup rsync would copy Dungeon Scriber's PVCs in plaintext"
    ++ lib.optional (
      !lib.hasInfix ''|| [ "$kns" = dungeon-scriber ]; then'' backup
    ) "Dungeon Scriber's database dump is not forced into the age-encrypted branch";
in
if problems != [ ] then
  throw "Dungeon Scriber deployment contract: ${lib.concatStringsSep "; " problems}"
else
  pkgs.runCommand "dungeon-scriber-deployment-contract"
    {
      nativeBuildInputs = [ pkgs.yq-go ];
      declaredNode = release.placement.nodeName;
      inherit fixtureNode;
      port = toString fixturePort;
    }
    ''
      set -euo pipefail
      fail() { echo "dungeon-scriber-deployment-contract: $*" >&2; exit 1; }
      q() { yq -N "$1" "$2"; }
      none() { [ -z "$(yq -N "[.. | select((tag == \"!!map\") and has(\"$1\"))] | length" "$2" | grep -vx 0)" ]; }
      api() { q "select(.kind == \"Deployment\") | $1" "$2"; }

      check_common() {
        f="$1"; node="$2"
        count=$(q '.kind' "$f" | grep -c . || true)
        [ "$count" -ge 12 ] || fail "only $count objects parsed from $f; extraction is broken"
        [ -z "$(q 'select(.kind == "Secret" or .kind == "Ingress" or .kind == "IngressRoute") | .kind' "$f")" ] \
          || fail "a Secret or ingress object is declared in the public manifest"
        [ -z "$(q 'select(.spec.type == "LoadBalancer") | .metadata.name' "$f")" ] || fail "LoadBalancer Service"
        none secretKeyRef "$f" || fail "secretKeyRef env persists secrets in containerd metadata"
        none hostPort "$f" || fail "hostPort declared; exposure is the tailnet NodePort only"

        q 'select(.spec.template) | .metadata.name + "=" + (.spec.template.spec.nodeSelector."kubernetes.io/hostname" // "")' "$f" \
          | while IFS== read -r name value; do
              [ "$value" = "$node" ] || fail "$name is pinned to '$value', not $node"
            done
        [ "$(q 'select(.kind == "PersistentVolume") | .spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]' "$f")" = "$node" ] \
          || fail "the blob PersistentVolume is not pinned to $node"
        [ "$(q 'select(.kind == "PersistentVolume") | .spec.persistentVolumeReclaimPolicy' "$f")" = Retain ] \
          || fail "the blob PersistentVolume must be Retain"

        q '.. | select((tag == "!!map") and has("image")) | .image' "$f" | while read -r image; do
          case "$image" in *@sha256:*) ;; *) fail "mutable image reference $image" ;; esac
        done

        [ "$(q 'select(.kind == "PersistentVolumeClaim" and .metadata.name == "postgres-data") | .spec.storageClassName' "$f")" = local-path-retain ] \
          || fail "postgres-data is not on local-path-retain"

        # The API refuses an unmounted dataset, then an old schema, before it serves.
        [ "$(api '.spec.template.spec.initContainers[0].name' "$f")" = require-blob-dataset ] || fail "no blob dataset gate"
        api '.spec.template.spec.initContainers[0].args[0]' "$f" | grep -q '.dungeon-scriber-blob-root' \
          || fail "the blob dataset gate must require the sentinel"
        [ "$(api '.spec.template.spec.initContainers[1].name' "$f")" = require-current-schema ] || fail "no schema gate"
        api '.spec.template.spec.initContainers[1].args[0]' "$f" | grep -q 'migrate.js --check' \
          || fail "the API schema gate must be migrate.js --check"
        api '.spec.template.metadata.annotations."dungeon-scriber.saldivar.io/config-sha256"' "$f" | grep -qE '^[0-9a-f]{64}$' \
          || fail "the API Pod template carries no settings hash, so a ConfigMap change would not roll it"
        [ "$(q 'select(.kind == "NetworkPolicy" and .metadata.name == "api-ingress") | .spec.podSelector.matchLabels.app' "$f")" = dungeon-scriber-api ] \
          || fail "the API has no ingress NetworkPolicy"

        q 'select(.spec.selector.matchLabels) | .metadata.name + " " + (.spec.selector.matchLabels | to_entries | map(.key + "=" + .value) | join(",")) + " " + (.spec.template.metadata.labels | to_entries | map(.key + "=" + .value) | join(","))' "$f" \
          | while read -r name sel labels; do
              IFS=, read -ra pairs <<< "$sel"
              for p in "''${pairs[@]}"; do
                case ",$labels," in *",$p,"*) ;; *) fail "$name selector $p is not in its template labels" ;; esac
              done
            done
      }

      ingress() { q 'select(.metadata.name == "api-ingress") | .spec.ingress | length' "$1"; }
      hops() { q 'select(.kind == "ConfigMap") | .data.TRUST_PROXY_HOPS' "$1"; }
      confighash() { api '.spec.template.metadata.annotations."dungeon-scriber.saldivar.io/config-sha256"' "$1"; }

      check_common ${renders.declared} "$declaredNode"
      for f in ${renders.off} ${renders.direct} ${renders.https} ${renders.draining} ${renders.public} ${renders.publicOnly}; do
        check_common "$f" "$fixtureNode"
      done

      # The settings hash covers the COMPLETE ConfigMap data, every key.
      for f in ${renders.declared} ${renders.off} ${renders.direct} ${renders.https} ${renders.draining}; do
        for key in NODE_ENV HOST PORT BLOB_ROOT TRUST_PROXY_HOPS LOG_LEVEL DEFAULT_ENTITLEMENTS AUTH_LOGIN_RATE_LIMIT_MAX AUTH_ROTATION_RATE_LIMIT_MAX; do
          [ -n "$(q "select(.kind == \"ConfigMap\") | .data.$key // \"\"" "$f")" ] || fail "ConfigMap lacks $key"
        done
        data_hash=$(q 'select(.kind == "ConfigMap") | .data' "$f" | yq -o=json -I=0 '.' | tr -d '\n' | sha256sum | cut -d' ' -f1)
        [ "$data_hash" = "$(confighash "$f")" ] || fail "config-sha256 is not the hash of the complete ConfigMap data"
      done

      # All off: nothing runs, nothing is exposed, nothing is admitted.
      [ -z "$(q 'select(.spec.replicas != null and .spec.replicas != 0) | .metadata.name' ${renders.off})" ] \
        || fail "an all-off release renders non-zero replicas"
      [ "$(q 'select(.kind == "Job") | .spec.suspend' ${renders.off})" = true ] || fail "all-off migration Job not suspended"
      [ "$(q 'select(.metadata.name == "api-tailnet") | .spec.type' ${renders.off})" = ClusterIP ] \
        || fail "api-tailnet exposed with every gate off"
      [ "$(ingress ${renders.off})" = 0 ] || fail "the API admits direct clients with every gate off"

      # Exposed, either way: one API, a running migration, a Local NodePort on the gated port.
      for f in ${renders.direct} ${renders.https}; do
        [ "$(api '.spec.replicas' "$f")" = 1 ] || fail "enabled API is not one replica"
        [ "$(q 'select(.kind == "Job") | .spec.suspend' "$f")" = false ] || fail "enabled migration Job suspended"
        [ "$(q 'select(.metadata.name == "api-tailnet") | .spec.type' "$f")" = NodePort ] || fail "no tailnet NodePort"
        [ "$(q 'select(.metadata.name == "api-tailnet") | .spec.externalTrafficPolicy' "$f")" = Local ] \
          || fail "tailnet NodePort must be externalTrafficPolicy Local"
        [ "$(q 'select(.metadata.name == "api-tailnet") | .spec.ports[0].nodePort' "$f")" = "$port" ] \
          || fail "tailnet NodePort differs from the gated port $port"
      done

      # Direct tailnet: tailnet clients on 3001 only, and no proxy hop is trusted.
      [ "$(ingress ${renders.direct})" = 1 ] || fail "direct exposure admits no tailnet client"
      [ "$(q 'select(.metadata.name == "api-ingress") | .spec.ingress[0].from[0].ipBlock.cidr' ${renders.direct})" = 100.64.0.0/10 ] \
        || fail "direct exposure must admit only the tailnet client range"
      [ "$(q 'select(.metadata.name == "api-ingress") | .spec.ingress[0].ports[0].port' ${renders.direct})" = 3001 ] \
        || fail "direct exposure must admit only the API port"
      [ "$(hops ${renders.direct})" = 0 ] || fail "a directly reachable API must trust no proxy hop"

      # Serve: host path only, one trusted hop, and that change rolls the Pod.
      [ "$(ingress ${renders.https})" = 0 ] || fail "with Serve on, the API still admits direct tailnet clients"
      [ "$(hops ${renders.https})" = 1 ] || fail "behind Serve the API must trust exactly one hop"
      [ "$(confighash ${renders.direct})" != "$(confighash ${renders.https})" ] \
        || fail "changing TRUST_PROXY_HOPS does not change the Pod template"

      # Draining: Serve still fronts it and the policy stays closed while the API rolls to 0.
      [ "$(ingress ${renders.draining})" = 0 ] || fail "the policy opens before tailnet.https is lowered"
      [ "$(hops ${renders.draining})" = 0 ] || fail "the draining state must trust no hop"
      [ "$(confighash ${renders.draining})" != "$(confighash ${renders.https})" ] \
        || fail "rolling to 0 hops does not change the Pod template"

      # Public route: exactly the configured Traefik Pods, on 3001, and nothing else;
      # the tailnet side is unchanged by it (Serve still closes the direct path).
      for f in ${renders.public} ${renders.publicOnly}; do
        [ "$(ingress "$f")" = 1 ] || fail "the public route must add exactly one ingress rule"
        pol() { q "select(.metadata.name == \"api-ingress\") | .spec.ingress[0].$1" "$f"; }
        [ "$(pol 'from[0].namespaceSelector.matchLabels."kubernetes.io/metadata.name"')" = fixture-ingress ] \
          || fail "the public rule must select the configured ingress namespace"
        [ "$(pol 'from[0].podSelector.matchLabels.app')" = fixture-proxy ] \
          || fail "the public rule must select the configured ingress Pods"
        [ "$(pol 'from | length')" = 1 ] || fail "the public rule must have one combined peer"
        [ "$(pol 'from[0].ipBlock // "none"')" = none ] || fail "the public rule must not admit an address range"
        [ "$(pol 'ports[0].port')" = 3001 ] || fail "the public rule must admit only the API port"
        [ "$(hops "$f")" = 1 ] || fail "behind Traefik the API must trust one hop"
      done
      [ "$(q 'select(.metadata.name == "api-tailnet") | .spec.type' ${renders.public})" = NodePort ] \
        || fail "the public route changed the tailnet Service"
      [ "$(q 'select(.metadata.name == "api-tailnet") | .spec.type' ${renders.publicOnly})" = ClusterIP ] \
        || fail "the public route exposed the tailnet NodePort"
      [ "$(confighash ${renders.public})" = "$(confighash ${renders.https})" ] \
        || fail "publishing must not roll the API when the hop count is unchanged"
      [ "$(api '.spec.template' ${renders.public} | sha256sum)" = "$(api '.spec.template' ${renders.https} | sha256sum)" ] \
        || fail "publishing changed the API Pod template"

      # The rendered public router explicitly denies the worker protocol and readiness.
      grep -F 'rule: ' ${publicRouteFile} | grep -qF ' && !PathPrefix(`/internal`)' \
        || fail "the rendered public rule lacks !PathPrefix(/internal)"
      grep -F 'rule: ' ${publicRouteFile} | grep -qF ' && !Path(`/ready`)' \
        || fail "the rendered public rule lacks !Path(/ready)"
      [ "$(grep -c 'middlewares: \[' ${publicRouteFile})" = 1 ] && grep -qF 'middlewares: ["k8s-dungeon-scriber-headers@file"]' ${publicRouteFile} \
        || fail "the rendered public router must use exactly its headers middleware"

      echo "Dungeon Scriber manifests: node- and digest-pinned; all-off, direct, Serve, draining and public shapes verified."
      touch $out
    ''
