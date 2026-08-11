# Declarative traefik routes for services that have migrated to k3s.
#
# WHY THIS EXISTS
# ---------------
# Public ingress stays on the docker `traefik` container until Phase 6, so every
# service that migrates needs a traefik route pointing at its Pod. Those routes were
# being hand-written into traefik's file-provider directory — which meant each
# migration was half declarative (the k8s manifest, in git) and half not (the route,
# a file on one host).
#
# That split is the dangerous kind. The manifest and the route are two halves of ONE
# change: a Deployment nobody can reach is not a migrated service. Losing the route
# leaves a service unreachable with nothing in git to explain why, and rebuilding
# minas from config would silently produce a cluster where every migrated service is
# a 404.
#
# So routes live here, beside the manifests, and each migration is one commit.
#
# ⚠️ ONE COMMIT, TWO HOSTS — THE DEPLOY ORDER MATTERS
# ---------------------------------------------------
# A migration commit touches files owned by DIFFERENT hosts, and rebuilding one
# does not deploy the other:
#
#   this file            → installed by MINAS' activation      (the route)
#   pelargir/manifests.nix → delivered from PELARGIR's auto-deploy dir (the Pod)
#
# So `nixos-rebuild switch` on minas alone publishes routes for Deployments that
# do not exist yet. **Rebuild pelargir FIRST**, confirm the Deployments are
# present, and only then rebuild minas. The `priority: 1` below is what keeps
# getting this wrong from causing an outage rather than merely being untidy.
#
# WHAT THIS DOES NOT MANAGE
# -------------------------
# `traefik.yml` in the same directory is hand-maintained and left alone. It holds the
# middleware definitions (basic-auth, stripPrefix chains) and legacy routes to
# external hosts. Only files matching `k8s-*.yml` belong to this module — the prefix
# is what makes "mine vs theirs" decidable, and it is why stale-file reporting below
# can be safe.
{ config, lib, pkgs, ... }:
let
  # The directory traefik bind-mounts as its file provider. Changing this means
  # changing the bind mount in docker/infra/docker-compose.yaml too.
  routeDir = "/home/edgar/git/docker/infra/traefik";

  # Authentik has deliberately separate publication and protection switches. Publishing
  # its own login route does not protect an application; attaching ForwardAuth does.
  # Phase A has accepted the public identity endpoint, while Phase B still starts with
  # no protected applications and edits ONE entry per accepted rollout change.
  authentikRollout = {
    publish = true;
    protectedRoutes = [ ];
    protectDashboard = false;
  };

  # Only administrator-facing applications belong here. Native/user-facing identity
  # services (Plex, Jellyfin, Overseerr, Nextcloud, Immich, etc.) deliberately retain
  # their own login flows and are not ForwardAuth candidates.
  authentikCandidateRoutes = [
    "maintainerr"
    "sonarr"
    "radarr"
    "lidarr"
    "animearr"
    "prowlarr"
    "deluge-vpn"
    "deluge-books"
    "gluetun"
  ];

  # ---------------------------------------------------------------------------
  #  Migrated services. Add a row here in the SAME commit as the k8s manifest.
  # ---------------------------------------------------------------------------
  # hosts       — public hostnames. A LIST: several services answer on more than one
  #               (overseerr on requests+overseer, wrapperr on stats+wrapperr), and a
  #               single-host field would silently drop the second, leaving a hostname
  #               that resolves publicly and 404s.
  # namespace   — k8s namespace.
  # port        — the Service port (not the container port).
  # middlewares — optional traefik middlewares, referenced with their provider suffix
  #               (e.g. "basic-auth@file"). Dropping these on migration is a SECURITY
  #               regression, not a cosmetic one: maintainerr's docker route carries
  #               basic-auth@file, and translating it without would publish an
  #               unauthenticated admin UI.
  routes = {
    # Always render this managed filename. While publish=false it contains `http: {}`;
    # that means a rollback overwrites a formerly-live route instead of merely reporting
    # it as stale and leaving Traefik to keep serving it.
    authentik = {
      hosts = [ "auth.saldivar.io" ];
      namespace = "authentik";
      serviceName = "authentik-server";
      port = 9000;
      enabled = authentikRollout.publish;
    };
    audiobookshelf = {
      hosts = [ "listen.saldivar.io" ];
      namespace = "books";
      port = 80;
    };
    kavita = {
      hosts = [ "books.saldivar.io" ];
      namespace = "books";
      port = 5000;
    };
    komga = {
      hosts = [ "komga.saldivar.io" ];
      namespace = "media";
      port = 25600;
    };
    # Atomic media wave. These land together because their saved application
    # configuration uses bare names that only resolve within the shared namespace.
    tautulli = {
      hosts = [ "tautulli.saldivar.io" ];
      namespace = "media";
      port = 8181;
    };
    overseerr = {
      # Keep both public names: each already resolves, so omitting either turns a
      # previously valid URL into a silent 404 after migration.
      hosts = [ "requests.saldivar.io" "overseer.saldivar.io" ];
      namespace = "media";
      port = 5055;
    };
    # deluge-books, STAGED EARLY on purpose (2026-08-09 cutover). At priority 1 this
    # can never outrank the docker container's own router (~26 by rule length), so
    # while docker is up it keeps btbooks.saldivar.io and this route is inert. The
    # instant docker stops, its router disappears and this one is the only match left.
    # The handover needs no timing — which is exactly why installing it BEFORE the
    # cutover removes a host activation from the downtime window.
    # deluge-vpn, staged early for the same reason as deluge-books below: at priority 1
    # it cannot outrank the docker container's own router, so it is inert until docker
    # stops and then takes over with no timing. Without this, `bt.saldivar.io` 404s the
    # moment the container stops, even after the Service takeover succeeds.
    # books-dl -> the netns trio's qbittorrent WebUI. ⚠️ The Service is named `gluetun`
    # (readmeabook's configured download-client host), and the renderer derives the backend
    # DNS from THIS KEY — so the key must be `gluetun`, in namespace `books`, port 8080.
    # nextcloud, staged early at priority 1 so it is inert until the docker container
    # stops and then takes over with no timing. ⚠️ port 80: the container serves plain HTTP
    # and OVERWRITEPROTOCOL=https is what makes it emit https:// URLs behind traefik.
    nextcloud = {
      hosts = [ "drive.saldivar.io" ];
      namespace = "nextcloud";
      port = 80;
    };
    # immich is staged early at generated priority 1, so the existing docker router
    # continues to win until cutover. ✅ Its measured ingress baseline is 200 and the
    # docker route sends HTTPS traffic to the app's container port 8080.
    immich = {
      hosts = [ "immich.saldivar.io" ];
      namespace = "immich";
      port = 8080;
    };
    gluetun = {
      hosts = [ "books-dl.saldivar.io" ];
      namespace = "books";
      port = 8080;
    };
    deluge-vpn = {
      hosts = [ "bt.saldivar.io" ];
      namespace = "media";
      port = 8112;
    };
    deluge-books = {
      hosts = [ "btbooks.saldivar.io" ];
      namespace = "media";
      port = 8112;
    };
    prowlarr = {
      hosts = [ "prowlarr.saldivar.io" ];
      namespace = "media";
      port = 9696;
    };
    sonarr = {
      hosts = [ "sonarr.saldivar.io" ];
      namespace = "media";
      port = 8989;
    };
    radarr = {
      hosts = [ "radarr.saldivar.io" ];
      namespace = "media";
      port = 7878;
    };
    lidarr = {
      hosts = [ "lidarr.saldivar.io" ];
      namespace = "media";
      port = 8686;
    };
    animearr = {
      hosts = [ "anime.saldivar.io" ];
      namespace = "media";
      port = 8989;
    };
    maintainerr = {
      hosts = [ "maintainerr.saldivar.io" ];
      namespace = "media";
      port = 6246;
      # Load-bearing: the application exposes an admin UI and the docker route
      # already protects it. Dropping this would publish that UI unauthenticated.
      middlewares = [ "basic-auth@file" ];
    };
    wrapperr = {
      hosts = [ "stats.saldivar.io" "wrapperr.saldivar.io" ];
      namespace = "media";
      port = 8282;
    };
    shelfmark = {
      hosts = [ "requestbooks.saldivar.io" ];
      namespace = "media";
      port = 8084;
    };
    # The docker label set carries no middlewares, so neither does this. Its
    # `loadbalancer.server.port` is 8096 — the HTTP port; 8920 is jellyfin's own
    # HTTPS listener and is reached on the host port, not through traefik.
    jellyfin = {
      hosts = [ "jellyfin.saldivar.io" ];
      namespace = "media";
      port = 8096;
    };
    # Baseline is 401, NOT 200 — plex answers unauthenticated requests at `/` with 401
    # and that is healthy. Verify against 401 after cutover; a 200 here would mean
    # something else is answering.
    plex = {
      hosts = [ "plex.saldivar.io" ];
      namespace = "media";
      port = 32400;
    };
    # `books`, not `media` — it lands beside audiobookshelf, which is its configured
    # backend and the only one of its three peers it can resolve natively. Baseline 200.
    readmeabook = {
      hosts = [ "bookrequests.saldivar.io" ];
      namespace = "books";
      port = 3030;
    };
    # `media`, because its one bare-name edge is `http://tautulli:8181` and tautulli
    # lives there — so it resolves natively with no alias object. Baseline 200.
    # NOTE the route key is `tracearr` while the docker router was named `trace`; the
    # generated router is `k8s-tracearr`, which cannot collide with `trace@docker`.
    tracearr = {
      hosts = [ "trace.saldivar.io" ];
      namespace = "media";
      port = 3000;
    };
  };

  # Backends are addressed by CLUSTER DNS NAME, never ClusterIP. traefik forwards
  # unknown names to CoreDNS (`dns:` in docker/infra/docker-compose.yaml), so a
  # Service can be deleted and recreated — taking a new ClusterIP — without this
  # route silently pointing into a black hole.
  # Traefik rule syntax: Host(`a`) || Host(`b`)
  #
  # NOTE: `middlewares` is emitted UNCONDITIONALLY as a JSON flow sequence, so an empty
  # list renders `middlewares: []` — which traefik accepts as "none".
  #
  # The obvious alternative, conditionally appending the block, was tried and produced
  # BROKEN YAML twice: Nix strips common leading indentation from '' blocks but not
  # from interpolated values, so the key landed at the wrong depth and traefik rejected
  # the file with "mapping values are not allowed in this context". An earlier variant
  # was worse — it parsed as a sibling of `routers`, so traefik accepted the file and
  # SILENTLY DROPPED the middleware, which for a basic-auth route means publishing an
  # unauthenticated admin UI that looks fine in review. Always-emit avoids the class.
  hostRule = hs: lib.concatMapStringsSep " || " (h: "Host(`${h}`)") hs;

  effectiveMiddlewares = name: r:
    (r.middlewares or [ ])
    ++ lib.optional
      (builtins.elem name authentikRollout.protectedRoutes)
      "authentik-forward-auth@file";

  renderRoute = name: r:
    if r.enabled or true then
      pkgs.writeText "k8s-${name}.yml" ''
    # GENERATED by hosts/nixos/minas-tirith/traefik-routes.nix — DO NOT EDIT HERE.
    # Edits are overwritten on the next nixos-rebuild. Change the Nix file instead.
    #
    # Router is named k8s-${name}, deliberately NOT ${name}: while the docker copy of
    # a migrated service still exists (stopped, as the rollback), restarting it
    # recreates ${name}@docker with the same Host rule. Distinct names keep rollback
    # unambiguous — and once mattered more than that, because two routers on one host
    # used to TIE and traefik picked between them arbitrarily, which is how the traefik
    # dashboard ended up unauthenticated for an unknown period. The explicit priority
    # below now settles that tie deterministically in docker's favour; the distinct
    # name remains so the two are still tellable apart in logs and the dashboard.
    #
    # entryPoint is `https` — the real name. `websecure` does not exist on this
    # traefik; the names in traefik.yml are static config sitting in the dynamic
    # directory and are silently ignored.
    http:
      routers:
        k8s-${name}:
          rule: "${hostRule r.hosts}"
          entryPoints: [https]
          # priority 1 — the LOWEST. This is load-bearing, and it is what makes
          # installing a route BEFORE its Pod exists a safe thing to do.
          #
          # Traefik defaults a router's priority to the LENGTH OF ITS RULE, so a
          # docker router for the same host lands around 26 and this one, at 1,
          # can never outrank it. While the docker container is up it keeps the
          # hostname; the instant it stops, its router disappears and this one is
          # the only match left. The handover needs no timing.
          #
          # Without this the two routers tie, and traefik picks between equal
          # priorities arbitrarily. That is not theoretical: on 2026-08-07 a
          # minas rebuild installed the ten `media` wave routes while their
          # Deployments did not yet exist (manifests.nix is delivered from
          # PELARGIR, and only minas had been rebuilt). Traefik chose the dead
          # k8s router for three hostnames and requests/overseer/lidarr returned
          # 502 while their docker containers were healthy the whole time.
          #
          # Safe here because nothing else matches these hostnames — the only
          # other explicit priorities in traefik.yml (90/100) are on
          # dungeon.saldivar.io. Check that again before adding a catch-all
          # router, which would outrank every route in this file.
          priority: 1
          service: k8s-${name}
          middlewares: ${builtins.toJSON (effectiveMiddlewares name r)}
      services:
        k8s-${name}:
          loadBalancer:
            servers:
              - url: "http://${r.serviceName or name}.${r.namespace}.svc.cluster.local:${toString r.port}"
      ''
    else
      pkgs.writeText "k8s-${name}.yml" ''
        # GENERATED disabled route. Keeping the file managed makes rollback remove a
        # previously-published router instead of leaving stale dynamic configuration.
        http: {}
      '';

  rendered = lib.mapAttrsToList (name: r: {
    inherit name;
    file = renderRoute name r;
  }) routes;

  validProtectedRoutes = lib.filter
    (name: builtins.hasAttr name routes)
    authentikRollout.protectedRoutes;

  authentikProtectedHosts = lib.unique (
    lib.concatMap (name: routes.${name}.hosts) validProtectedRoutes
    ++ lib.optional authentikRollout.protectDashboard "traefik.saldivar.io"
  );

  authentikGateEnabled = authentikProtectedHosts != [ ];
  authentikServerUrl = "http://authentik-server.authentik.svc.cluster.local:9000";

  # JSON is valid YAML and avoids indentation-sensitive optional blocks. The callback
  # router is load-bearing: browser requests to a protected application's
  # /outpost.goauthentik.io/ path must bypass ForwardAuth and reach the embedded outpost.
  authentikGateConfig = if authentikGateEnabled then {
    http = {
      middlewares.authentik-forward-auth.forwardAuth = {
        address = "${authentikServerUrl}/outpost.goauthentik.io/auth/traefik";
        trustForwardHeader = true;
        authResponseHeaders = [
          "X-authentik-username"
          "X-authentik-groups"
          "X-authentik-entitlements"
          "X-authentik-email"
          "X-authentik-name"
          "X-authentik-uid"
          "X-authentik-jwt"
          "X-authentik-meta-jwks"
          "X-authentik-meta-outpost"
          "X-authentik-meta-provider"
          "X-authentik-meta-app"
          "X-authentik-meta-version"
        ];
      };
      routers = {
        authentik-callback = {
          rule = "(${hostRule authentikProtectedHosts}) && PathPrefix(`/outpost.goauthentik.io/`)";
          entryPoints = [ "https" ];
          priority = 10000;
          service = "authentik-outpost";
        };
      } // lib.optionalAttrs authentikRollout.protectDashboard {
        authentik-traefik-dashboard = {
          rule = "Host(`traefik.saldivar.io`)";
          entryPoints = [ "https" ];
          priority = 9000;
          service = "api@internal";
          # Keep the known BasicAuth fallback through the first real acceptance.
          middlewares = [ "basic-auth@file" "authentik-forward-auth@file" ];
        };
      };
      services.authentik-outpost.loadBalancer.servers = [
        { url = authentikServerUrl; }
      ];
    };
  } else {
    http = { };
  };

  authentikGateFile = pkgs.writeText "k8s-authentik-gate.yml" ''
    # GENERATED by hosts/nixos/minas-tirith/traefik-routes.nix — DO NOT EDIT.
    ${builtins.toJSON authentikGateConfig}
  '';

  managedNames = (map (e: "k8s-${e.name}.yml") rendered) ++ [ "k8s-authentik-gate.yml" ];
in
{
  assertions = [
    {
      assertion = lib.all
        (name: builtins.elem name authentikCandidateRoutes)
        authentikRollout.protectedRoutes;
      message = "authentikRollout.protectedRoutes contains a non-admin or unknown route";
    }
    {
      assertion = lib.all
        (name: builtins.hasAttr name routes)
        authentikRollout.protectedRoutes;
      message = "authentikRollout.protectedRoutes names a route that does not exist";
    }
    {
      assertion = !authentikGateEnabled || authentikRollout.publish;
      message = "Authentik ForwardAuth cannot be enabled before auth.saldivar.io is published";
    }
  ];

  system.activationScripts.minas-traefik-routes = lib.stringAfter [ "users" ] ''
    if [ -d ${routeDir} ]; then
      ${lib.concatMapStringsSep "\n" (e: ''
        install -m 0644 -o edgar -g users ${e.file} ${routeDir}/k8s-${e.name}.yml
      '') rendered}
      install -m 0644 -o edgar -g users ${authentikGateFile} ${routeDir}/k8s-authentik-gate.yml

      # Report k8s-*.yml files that are no longer declared. Deliberately REPORT and
      # not delete: traefik would drop the route instantly, taking a live service
      # offline mid-activation, and a rebuild is not where an outage should originate.
      # Removing a service means removing it here AND deleting the file by hand, in
      # that order, having checked nothing still needs it.
      expected="${lib.concatStringsSep " " managedNames}"
      for f in ${routeDir}/k8s-*.yml; do
        [ -e "$f" ] || continue
        b=$(basename "$f")
        case " $expected " in
          *" $b "*) ;;
          *) echo "WARNING: stale traefik route $b is no longer declared in traefik-routes.nix." >&2
             echo "         traefik is STILL SERVING it. Delete it by hand once confirmed unused." >&2 ;;
        esac
      done
    else
      echo "WARNING: ${routeDir} missing — traefik routes for migrated services not installed." >&2
    fi
  '';
}
