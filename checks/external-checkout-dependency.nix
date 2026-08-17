# The fleet still depends on a checkout that is not this repository.
#
# `/home/edgar/git/docker` is the pre-migration Docker Compose tree. Its
# CONTAINERS are gone — docker runs zero — but its DIRECTORY was never migrated,
# and it is now the on-disk config store for live k3s workloads, the drop point
# for every generated ingress route, AND the location of a database the nightly
# backup dumps.
#
# That is the largest structural problem in the fleet: a "completed" migration
# whose cluster cannot survive `rm -rf` on the thing it migrated away from.
#
# This check does not fix it. It makes the problem impossible to worsen silently
# and impossible to misreport:
#
#   1. It DISCOVERS bindings by walking the whole hosts/ tree, rather than
#      trusting a hand-written file list. The first version of this check scanned
#      four named files and therefore missed the fifth binding entirely — the one
#      in the backup program — while confidently reporting "4 of 4". Discovery
#      must not depend on the author already knowing the answer.
#   2. It matches the EXACT set of file/line pairs, not a count. Comparing totals
#      would let one dependency be removed and another added with the check still
#      green.
#
# ✅ THE LIST IS NOW EMPTY — 2026-08-16. Nothing in this repository binds that
# directory any more: immich and shelfmark moved to /usr/local/etc/<service>, the
# hand-maintained traefik.yml was deleted, and the traefik file provider moved to
# /usr/local/etc/traefik.
#
# The check is deliberately KEPT while empty. It now guards against regression: any
# new binding on that path fails the build, which is exactly what you want while the
# old directory still exists as the rollback.
#
# Delete this file AND /home/edgar/git/docker together, once the rollback window has
# passed and nothing has needed it.
{
  lib,
  pkgs,
  ...
}:
let
  externalRoot = "/home/edgar/git/docker";
  hostsRoot = ../hosts;

  # Walk every Nix and YAML file under hosts/. A file this misses is a binding
  # that goes unnoticed, which is the failure this check exists to prevent.
  collect =
    dir:
    let
      entries = builtins.readDir dir;
      go =
        name: type:
        let
          path = dir + "/${name}";
        in
        if type == "directory" then
          collect path
        else if
          type == "regular"
          && (lib.hasSuffix ".nix" name || lib.hasSuffix ".yaml" name || lib.hasSuffix ".yaml.in" name)
        then
          [ path ]
        else
          [ ];
    in
    lib.flatten (lib.mapAttrsToList go entries);

  sourceFiles = collect hostsRoot;

  relative = p: lib.removePrefix (toString ../. + "/") (toString p);

  # A BINDING is a line naming the external root outside a comment. Comment forms
  # that occur here: a Nix `#` line and a YAML `#` line, both leading.
  bindingsIn =
    path:
    let
      lines = lib.splitString "\n" (builtins.readFile path);
      isComment = line: lib.hasPrefix "#" (lib.trim line);
      hits = lib.filter (line: lib.hasInfix externalRoot line && !(isComment line)) lines;
    in
    map (line: "${relative path}: ${lib.trim line}") hits;

  found = lib.sort (a: b: a < b) (lib.flatten (map bindingsIn sourceFiles));

  # The exhaustive set as of 2026-08-16, each with what must happen before it goes.
  #
  #   traefik-routes.nix routeDir      minas activation writes the 22 generated route
  #                                    files here; the traefik Pod mounts the same
  #                                    directory. Resolve by rendering the file
  #                                    provider into the store and mounting that.
  #   traefik.yaml file-provider       hostPath, type: Directory. Goes WITH the above;
  #                                    they are two halves of one dependency.
  #   immich.yaml config               hostPath, type: Directory. Independent; relocate
  #                                    the config tree and restart immich.
  #   shelfmark.yaml config            hostPath, type: Directory.
  #   backup-root-data.nix users.db    the nightly backup DUMPS a SQLite database out
  #                                    of that tree. Moving the shelfmark config must
  #                                    move this declaration with it, or the dump
  #                                    silently stops.
  expected = lib.sort (a: b: a < b) [
  ];

  added = lib.subtractLists expected found;
  removed = lib.subtractLists found expected;
  fmt = xs: lib.concatMapStringsSep "\n  " (x: x) xs;
in
if added != [ ] then
  throw ''
    A NEW dependency on ${externalRoot} was added:
      ${fmt added}

    That directory is the pre-migration Docker checkout. It is not part of this
    repository, it is not managed as config, and deleting it takes down every
    workload bound to it. The fleet is trying to get OFF it, not further onto it.

    If the new binding is genuinely necessary, add its exact line to `expected` in
    this file together with a note saying what must happen before it can go.
  ''
else if removed != [ ] then
  throw ''
    A dependency on ${externalRoot} was REMOVED — good — but this file still
    expects it:
      ${fmt removed}

    Delete the corresponding entry from `expected` so the remaining work stays
    accurate. When `expected` is empty, delete this check and the directory in the
    same change.
  ''
else
  pkgs.runCommand "external-checkout-dependency-ok" { } "touch $out"
