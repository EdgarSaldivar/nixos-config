{ pinCollectorRelease }:
let
  # Authentik has deliberately separate publication and protection switches.
  # Publishing its login route does not protect an application; attaching
  # ForwardAuth does. Each accepted protection wave stays explicit so rollback
  # can detach only the affected applications.
  authentikRollout = {
    publish = true;
    # Maintainerr was the browser-tested canary before the ARR administration
    # wave. Native ARR authentication remains enabled behind this edge gate.
    protectedRoutes = [
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
    protectDashboard = true;
  };
in
{
  inherit authentikRollout;

  # Only administrator-facing applications belong here. Native/user-facing
  # identity services deliberately retain their own login flows.
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

  # These have no acceptable native fallback. BasicAuth is attached only when an
  # operator explicitly detaches Authentik, never in the steady-state path.
  legacyBasicAuthFallbackRoutes = [
    "maintainerr"
    "lidarr"
  ];

  # Migrated services. Add a row here in the SAME commit as the k8s manifest.
  #
  # hosts       — public hostnames. A LIST: several services answer on more than
  #               one, and a single-host field would silently drop the second.
  # namespace   — k8s namespace.
  # port        — the Service port (not the container port).
  # middlewares — optional route-specific middlewares. Administrator authentication
  #               is derived centrally by the renderer so a route cannot silently
  #               omit the active Authentik gate or its rollback fallback.
  routes = {
    # Always render this filename. When disabled it overwrites the live router
    # with `http: {}` rather than leaving stale configuration served.
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
      # Both names resolve publicly; dropping either produces a silent 404.
      hosts = [
        "requests.saldivar.io"
        "overseer.saldivar.io"
      ];
      namespace = "media";
      port = 5055;
    };
    # deluge-books and deluge-vpn are staged early on purpose. At priority 1 these
    # routes cannot outrank their docker routers, so they remain inert until docker
    # stops and then take over without putting a host activation in the downtime
    # window.
    #
    # books-dl targets the netns trio's qBittorrent WebUI. The Service is named
    # `gluetun` (readmeabook's configured download-client host), and backend DNS is
    # derived from the route key, so that key and its books namespace are load-bearing.
    #
    # nextcloud is staged early for the same priority-based handoff. Port 80 is
    # intentional: the container serves HTTP and OVERWRITEPROTOCOL=https makes it
    # emit HTTPS URLs behind Traefik.
    nextcloud = {
      hosts = [ "drive.saldivar.io" ];
      namespace = "nextcloud";
      port = 80;
    };
    immich = {
      # Staged at priority 1. Its measured ingress baseline is 200 and the former
      # docker route also sent HTTPS ingress to the application's port 8080.
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
    };
    wrapperr = {
      hosts = [
        "stats.saldivar.io"
        "wrapperr.saldivar.io"
      ];
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
    # Baseline is 401, NOT 200 — plex answers unauthenticated requests at `/` with
    # 401 and that is healthy. A 200 would mean something else is answering.
    plex = {
      hosts = [ "plex.saldivar.io" ];
      namespace = "media";
      port = 32400;
    };
    # `books`, not `media` — it lands beside audiobookshelf, its configured backend
    # and the only one of its three peers it can resolve natively. Baseline 200.
    readmeabook = {
      hosts = [ "bookrequests.saldivar.io" ];
      namespace = "books";
      port = 3030;
    };
    # `media`, because its bare-name edge is `http://tautulli:8181`. The route key is
    # intentionally `tracearr`, unlike the old docker router `trace`, so generated
    # `k8s-tracearr` cannot collide with `trace@docker`.
    tracearr = {
      hosts = [ "trace.saldivar.io" ];
      namespace = "media";
      port = 3000;
    };
    pin-collector = {
      hosts = [ "pin.saldivar.io" ];
      namespace = "pin-collector";
      serviceName = "api";
      port = 8000;
      # Readiness performs dependency I/O for kubelet; keep it off the public
      # router even though in-process checks are separately bounded.
      excludedPaths = [ "/ready" ];
      enabled = pinCollectorRelease.enabled;
    };
  };
}
