# The fleet still depends on a checkout that is not this repository.
#
# `/home/edgar/git/docker` is the pre-migration Docker Compose tree. Its
# CONTAINERS are gone — docker runs zero — but its DIRECTORY was never migrated,
# and it is now the on-disk config store for three live k3s workloads plus the
# drop point for every generated ingress route.
#
# That is the largest structural problem in the fleet: a "completed" migration
# whose cluster still cannot survive `rm -rf` on the thing it migrated away from.
#
# This check does not fix it. It does two things that matter while it is being
# fixed:
#
#   1. NO NEW DEPENDENCIES. The set below is exhaustive as of 2026-08-16. Adding a
#      fourth binding fails the build, so the problem cannot quietly grow while
#      nobody is looking at it.
#   2. PROGRESS IS VISIBLE. Removing a dependency requires deleting its line here,
#      which makes each migration step a deliberate, reviewable act rather than
#      something discovered later by grep.
#
# When this list reaches zero, delete this file and the directory in the same
# change — and only then.
{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:
let
  externalRoot = "/home/edgar/git/docker";

  # Exhaustive, and each entry says what has to happen before it can go.
  expected = {
    "traefik-routes.nix routeDir" = ''
      minas activation writes the 22 generated route files here, and the traefik Pod
      bind-mounts the same directory as its file provider. Resolve by rendering the
      file provider into the Nix store and mounting that instead; the routes are
      already generated declaratively, they are simply written to the wrong place.
    '';
    "manifests/traefik.yaml file-provider volume" = ''
      hostPath, type: Directory. Goes at the same time as the routeDir above -- they
      are two halves of one dependency.
    '';
    "manifests/immich.yaml config volume" = ''
      hostPath, type: Directory. Independent of the others; relocate the config tree
      to a real data path and restart immich.
    '';
    "manifests/shelfmark.yaml config volume" = ''
      hostPath, type: Directory, and it holds users.db, which is a DECLARED backup
      dump target. The backup declaration in system.nix moves with it.
    '';
  };

  # Count the real bindings from source rather than trusting the list above.
  sources = {
    "traefik-routes.nix" = builtins.readFile ../hosts/nixos/minas-tirith/traefik-routes.nix;
    "traefik.yaml" = builtins.readFile ../hosts/nixos/minas-tirith/manifests/traefik.yaml;
    "immich.yaml" = builtins.readFile ../hosts/nixos/minas-tirith/manifests/immich.yaml;
    "shelfmark.yaml" = builtins.readFile ../hosts/nixos/minas-tirith/manifests/shelfmark.yaml;
  };

  # A binding is a line that names the external root as a PATH, not in prose. Both
  # forms that occur: a Nix string assignment and a YAML `path:` value.
  bindingLines =
    text:
    lib.filter (
      line:
      lib.hasInfix externalRoot line
      && (lib.hasInfix "path:" line || lib.hasInfix "routeDir =" line)
      && !(lib.hasInfix "#" (lib.head (lib.splitString externalRoot line)))
    ) (lib.splitString "\n" text);

  found = lib.mapAttrs (_: bindingLines) sources;
  total = lib.foldl' (acc: v: acc + builtins.length v) 0 (lib.attrValues found);
  expectedTotal = builtins.length (lib.attrNames expected);

  report = lib.concatStringsSep "\n" (
    lib.mapAttrsToList (f: lines: "  ${f}: ${toString (builtins.length lines)}") found
  );
in
if total > expectedTotal then
  throw ''
    A NEW dependency on ${externalRoot} was added.

    That directory is the pre-migration Docker checkout. It is not part of this
    repository, it is not backed up as config, and deleting it takes down every
    workload bound to it. The fleet is trying to get OFF it, not further onto it.

    Expected ${toString expectedTotal} bindings, found ${toString total}:
    ${report}

    If the new binding is genuinely necessary, add it to `expected` in this file
    with a note saying what has to happen before it can go.
  ''
else if total < expectedTotal then
  throw ''
    A dependency on ${externalRoot} was REMOVED — good — but the list in this file
    still claims ${toString expectedTotal}.

    Found ${toString total}:
    ${report}

    Delete the corresponding entry from `expected` so the remaining work stays
    accurate. When it reaches zero, delete this check and the directory together.
  ''
else
  pkgs.runCommand "external-checkout-dependency-ok" { } "touch $out"
