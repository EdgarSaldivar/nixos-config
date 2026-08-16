# Public hostnames served by the docker `traefik` on minas-tirith (10.0.1.6).
#
# SINGLE SOURCE OF TRUTH — consumed by two places that must never disagree:
#
#   1. `containers.nix` → `networking.hosts`, so the HOST and its docker containers
#      resolve these names to the LAN address instead of hairpinning out through the
#      public IP and back in via NAT loopback.
#   2. `pelargir/manifests.nix` → the `coredns-custom` ConfigMap, so k3s PODS resolve
#      them the same way. Without it a migrated Pod resolves e.g. tautulli.saldivar.io
#      to the public address and its traffic leaves the network to come straight back —
#      fragile, slow, and dependent on the router's NAT loopback behaviour.
#
# Duplicating the list in both places would work until someone edited one of them, at
# which point host and cluster would silently disagree about where a service lives.
#
# ⛔ NAMED HOSTS ONLY — never a `*.saldivar.io` wildcard. `ha-pelargir.saldivar.io`
# lives on PELARGIR, and a blanket zone mapping would point Home Assistant's own
# hostname at minas and break it. Every entry below is deliberately a service that
# terminates on minas.
#
# Entries stay valid now that every service runs on k3s: traefik is still the single
# ingress for these names, so each resolves to 10.0.1.6 and traefik forwards to the
# Pod. The only cost is one extra hop for Pod→Pod traffic that could have used a
# cluster DNS name directly.
#
# NOTE: readarr.saldivar.io is deliberately absent — readarr was dropped entirely on
# 2026-08-06 (LinuxServer retired the image; upstream archived), its service block
# removed from the media stack and /etc/readarr deleted.
[
  "traefik.saldivar.io"
  "auth.saldivar.io"

  # media
  "plex.saldivar.io"
  "jellyfin.saldivar.io"
  "sonarr.saldivar.io"
  "radarr.saldivar.io"
  "lidarr.saldivar.io"
  "prowlarr.saldivar.io"
  "anime.saldivar.io"
  "overseer.saldivar.io"
  "requests.saldivar.io"
  "maintainerr.saldivar.io"
  "tautulli.saldivar.io"
  "wrapperr.saldivar.io"
  "stats.saldivar.io"
  "trace.saldivar.io"
  "bt.saldivar.io"

  # books
  "books.saldivar.io"
  "komga.saldivar.io"
  "listen.saldivar.io"
  "bookrequests.saldivar.io"
  "requestbooks.saldivar.io"
  "books-dl.saldivar.io"
  "btbooks.saldivar.io"

  # cloud / photos
  "drive.saldivar.io"
  "immich.saldivar.io"

  # PinCollector uses one origin. The admin UI lives under /admin; the retired
  # admin.pin.saldivar.io name is intentionally absent.
  "pin.saldivar.io"
]
