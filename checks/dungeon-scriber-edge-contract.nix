{
  lib,
  pkgs,
  ...
}:

# Dungeon Scriber's public edge, proved against a real Traefik rather than by reading
# the rule text. The route file is rendered by the PRODUCTION catalog and renderer
# (with public.enable forced on, so this holds whatever the deployed gate says), its
# backend is pointed at a local stub, and a Traefik from nixpkgs serves it on loopback
# with the same https entrypoint shape as minas. Then:
#
#   * only /v1/... and /health reach the backend; /, /ready and /internal/v1 do not,
#     including dot-segment and percent-encoded attempts to climb out of /v1/;
#   * HSTS (one year, no preload) and nosniff are on every routed response;
#   * a forged X-Forwarded-For from an untrusted peer is discarded, so the rightmost
#     (and only) entry the backend sees is the real peer;
#   * X-Request-Id passes through, and a 5 MB body streams through unchanged.
#
# nixpkgs' Traefik is not byte-for-byte the pinned production image, so this proves
# the configuration's behaviour on Traefik 3, not the live binary; the runbook repeats
# the path checks against the live edge after cutover.
let
  release = import ../hosts/nixos/minas-tirith/dungeon-scriber-release.nix;
  catalog = import ../hosts/nixos/minas-tirith/traefik-routes/catalog.nix {
    pinCollectorRelease = import ../hosts/nixos/minas-tirith/pin-collector-release.nix;
    dungeonScriberRelease = release // {
      public = release.public // {
        enable = true;
      };
    };
  };
  rendering = import ../hosts/nixos/minas-tirith/traefik-routes/render.nix {
    inherit lib pkgs;
    inherit (catalog) authentikRollout legacyBasicAuthFallbackRoutes routes;
  };
  routeFile = (lib.findFirst (e: e.name == "dungeon-scriber") null rendering.rendered).file;
  hostname = release.public.hostname;
  # The production trusted ranges, from their single source; loopback is not among them.
  trustedIPs = lib.concatStringsSep "," (import ../hosts/nixos/pelargir/cloudflare-ranges.nix).v4;

  backend = pkgs.writeText "backend.py" ''
    import http.server, json, sys
    class H(http.server.BaseHTTPRequestHandler):
        def _r(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            out = json.dumps({
                "path": self.path,
                "xff": self.headers.get("X-Forwarded-For"),
                "rid": self.headers.get("X-Request-Id"),
                "len": len(body),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)
        do_GET = _r
        do_POST = _r
        def log_message(self, *a):
            pass
    http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
  '';
in
pkgs.runCommand "dungeon-scriber-edge-contract"
  {
    nativeBuildInputs = [
      pkgs.traefik
      pkgs.python3
      pkgs.curl
      pkgs.gnused
      pkgs.gnugrep
      pkgs.coreutils
    ];
    inherit hostname trustedIPs;
  }
  ''
    set -euo pipefail
    fail() { echo "dungeon-scriber-edge-contract: $*" >&2; cat traefik.log >&2 || true; exit 1; }
    bport=18301; eport=18443

    mkdir dyn
    sed "s#http://api.dungeon-scriber.svc.cluster.local:3001#http://127.0.0.1:$bport#" ${routeFile} > dyn/route.yml
    grep -q "127.0.0.1:$bport" dyn/route.yml || fail "could not point the rendered route at the stub"

    python3 ${backend} "$bport" & bpid=$!
    # Same entrypoint shape as manifests/traefik.yaml: TLS on `https`, forwarded headers
    # trusted only from the production Cloudflare ranges, which loopback is not in.
    traefik --entrypoints.https.address=127.0.0.1:$eport --entrypoints.https.http.tls=true \
      --entrypoints.https.forwardedHeaders.trustedIPs="$trustedIPs" \
      --providers.file.directory="$PWD/dyn" --log.level=INFO > traefik.log 2>&1 & tpid=$!
    trap 'kill $tpid $bpid 2>/dev/null || true' EXIT

    req() { curl -sk --path-as-is --max-time 10 --resolve "$hostname:$eport:127.0.0.1" "$@"; }
    code() { req -o /dev/null -w '%{http_code}' "https://$hostname:$eport$1"; }

    for i in $(seq 1 50); do
      [ "$(code /health)" = 200 ] && break
      sleep 0.2
    done
    [ "$(code /health)" = 200 ] || fail "Traefik never routed /health"

    for p in /health /v1/campaigns /v1/campaigns/x/sessions; do
      [ "$(code "$p")" = 200 ] || fail "$p must reach the API"
    done
    for p in / /ready /ready/ /internal/v1/jobs /internal/v1/ /internal /v1evil /V1/campaigns \
             /Internal/v1/jobs //internal/v1/jobs /health/x \
             /v1/../internal/v1/jobs /v1/%2e%2e/internal/v1/jobs /v1/..%2finternal/v1/jobs \
             /v1/%2E%2E%2Finternal/v1/jobs /v1//../internal/v1/jobs /v1/%252e%252e/internal; do
      [ "$(code "$p")" = 404 ] || fail "$p must not reach the API (got $(code "$p"))"
    done

    req -D headers -o body -H 'X-Forwarded-For: 6.6.6.6' -H 'X-Request-Id: edge-check-1' \
      -X POST -H 'Content-Type: application/octet-stream' \
      --data-binary @<(head -c 5000000 /dev/zero) "https://$hostname:$eport/v1/upload"
    grep -qi '^strict-transport-security: max-age=31536000' headers || fail "no one-year HSTS"
    ! grep -qi '^strict-transport-security:.*\(preload\|includesubdomains\)' headers || fail "HSTS must not preload or cover subdomains"
    grep -qi '^x-content-type-options: nosniff' headers || fail "no nosniff"
    grep -q '"xff": "127.0.0.1"' body || fail "forged X-Forwarded-For survived: $(cat body)"
    grep -q '"rid": "edge-check-1"' body || fail "X-Request-Id was not passed through"
    grep -q '"len": 5000000' body || fail "the upload body did not arrive intact"

    echo "Dungeon Scriber edge: allowlist, HSTS, nosniff, forwarded-header and streaming behaviour verified."
    touch $out
  ''
