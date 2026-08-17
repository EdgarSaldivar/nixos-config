# Traefik manifests with the Cloudflare ranges substituted in.
#
# ⛔ THIS FILE EXISTS SO THERE IS EXACTLY ONE SUBSTITUTION, not several.
#
# It began inline in manifests.nix, with checks/cloudflare-ranges.nix
# re-implementing the same `replaceStrings` to verify it. Cross-review named the
# flaw: that is a common-mode failure. Breaking only the production substitution
# ships `trustedIPs=@cloudflareTrustedIPsV4@` to the LIVE public ingress while the
# check verifies its own private rendering and stays green -- so the mutation proof
# claimed for it did not hold.
#
# Three consumers now share this one expression:
#   * pelargir/manifests.nix   -- the delivered minas-traefik.yaml
#   * checks/cloudflare-ranges.nix -- verifies the real thing, not a copy
#   * flake `packages.<system>.traefik-canary` -- the maintenance canary, which is
#     applied BY HAND and therefore used to carry its own stale copy of the list
#
# `render` takes the source manifest so the canary cannot drift from production.
{ lib, pkgs }:
let
  ranges = import ./cloudflare-ranges.nix;

  render =
    name: src:
    pkgs.writeText name (
      builtins.replaceStrings [ "@cloudflareTrustedIPsV4@" ] [ (lib.concatStringsSep "," ranges.v4) ] (
        builtins.readFile src
      )
    );
in
{
  inherit render;
  minasTraefik = render "minas-traefik.yaml" ../minas-tirith/manifests/traefik.yaml;
  canary = render "traefik-canary.yaml" ../../../experiments/traefik-canary/traefik-canary.yaml;
}
