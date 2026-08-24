# Declarative traefik routes for services that have migrated to k3s.
#
# Every public service needs both a k3s workload and a file-provider route. These
# routes are delivered by MINAS while manifests are delivered by PELARGIR, so a
# migration commit still spans two hosts: rebuild pelargir first, confirm the
# Deployment, and only then rebuild minas. The renderer's explicit priority is
# what makes publishing the route before that second rebuild safe.
#
# Only explicit `k8s-*.yml` inventory entries belong to this module. A generated
# inventory is intentional: dynamic discovery cannot prove what a rebuilt host
# must contain, and disappearing provider files are not safe to prune during
# activation.
{
  lib,
  pkgs,
  ...
}:
let
  catalog = import ./traefik-routes/catalog.nix {
    pinCollectorRelease = import ./pin-collector-release.nix;
  };
  rendering = import ./traefik-routes/render.nix {
    inherit lib pkgs;
    inherit (catalog)
      authentikRollout
      legacyBasicAuthFallbackRoutes
      routes
      ;
  };
in
import ./traefik-routes/delivery.nix {
  inherit lib;
  inherit (catalog)
    authentikCandidateRoutes
    authentikRollout
    legacyBasicAuthFallbackRoutes
    routes
    ;
  inherit (rendering)
    authentikGateEnabled
    authentikGateFile
    managedNames
    rendered
    ;
}
