# Declarative traefik routes for services that have migrated to k3s.
#
# WHY THIS EXISTS
# ---------------
# Public ingress stays on the docker `traefik2` container until Phase 6, so every
# service that migrates needs a traefik route pointing at its Pod. Those routes were
# being hand-written into traefik2's file-provider directory — which meant each
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
# WHAT THIS DOES NOT MANAGE
# -------------------------
# `traefik.yml` in the same directory is hand-maintained and left alone. It holds the
# middleware definitions (basic-auth, stripPrefix chains) and legacy routes to
# external hosts. Only files matching `k8s-*.yml` belong to this module — the prefix
# is what makes "mine vs theirs" decidable, and it is why stale-file reporting below
# can be safe.
{ config, lib, pkgs, ... }:
let
  # The directory traefik2 bind-mounts as its file provider. Changing this means
  # changing the bind mount in docker/infra/docker-compose.yaml too.
  routeDir = "/home/edgar/git/docker/infra/traefik";

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
    audiobookshelf = {
      hosts = [ "listen.saldivar.io" ];
      namespace = "books";
      port = 80;
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
  };

  # Backends are addressed by CLUSTER DNS NAME, never ClusterIP. traefik2 forwards
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

  renderRoute = name: r: pkgs.writeText "k8s-${name}.yml" ''
    # GENERATED by hosts/nixos/minas-tirith/traefik-routes.nix — DO NOT EDIT HERE.
    # Edits are overwritten on the next nixos-rebuild. Change the Nix file instead.
    #
    # Router is named k8s-${name}, deliberately NOT ${name}: while the docker copy of
    # a migrated service still exists (stopped, as the rollback), restarting it
    # recreates ${name}@docker with the same Host rule. Two routers matching one host
    # have equal priority and traefik picks between them arbitrarily — which is how
    # the traefik dashboard ended up unauthenticated for an unknown period. Distinct
    # names keep rollback unambiguous.
    #
    # entryPoint is `https` — the real name. `websecure` does not exist on this
    # traefik; the names in traefik.yml are static config sitting in the dynamic
    # directory and are silently ignored.
    http:
      routers:
        k8s-${name}:
          rule: "${hostRule r.hosts}"
          entryPoints: [https]
          service: k8s-${name}
          middlewares: ${builtins.toJSON (r.middlewares or [])}
      services:
        k8s-${name}:
          loadBalancer:
            servers:
              - url: "http://${name}.${r.namespace}.svc.cluster.local:${toString r.port}"
  '';

  rendered = lib.mapAttrsToList (name: r: {
    inherit name;
    file = renderRoute name r;
  }) routes;

  managedNames = map (e: "k8s-${e.name}.yml") rendered;
in
{
  system.activationScripts.minas-traefik-routes = lib.stringAfter [ "users" ] ''
    if [ -d ${routeDir} ]; then
      ${lib.concatMapStringsSep "\n" (e: ''
        install -m 0644 -o edgar -g users ${e.file} ${routeDir}/k8s-${e.name}.yml
      '') rendered}

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
