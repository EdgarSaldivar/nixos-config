# Regression guard: nothing in this repository may bind `/home/edgar/git/docker`.
#
# ✅ THE DIRECTORY IS GONE — deleted 2026-08-16, along with the last binding.
#
# It was the pre-migration Docker Compose tree. Its containers had been stopped for
# months, but its DIRECTORY was never migrated, so it had quietly become the on-disk
# config store for live k3s workloads, the drop point for every generated ingress
# route, and the location of a database the nightly backup dumps. A "completed"
# migration whose cluster could not survive `rm -rf` on the thing it migrated away
# from. Everything now lives under /usr/local/etc/<service>.
#
# THIS CHECK IS KEPT, AND KEPT EMPTY, ON PURPOSE. The path no longer exists, so a
# new reference to it is not a regression toward a bad-but-working state — it is a
# reference to nothing, which fails at runtime on a host, not here. Failing the
# build is strictly better.
#
# Two properties are worth preserving if this is ever edited:
#
#   1. It DISCOVERS bindings by walking the whole hosts/ tree, rather than trusting
#      a hand-written file list. The first version scanned four named files and
#      therefore missed the fifth binding entirely — the one in the backup program —
#      while confidently reporting "4 of 4". Discovery must not depend on the author
#      already knowing the answer.
#   2. It matches the EXACT set of file/line pairs, not a count. Comparing totals
#      would let one dependency be removed and another added with the check still
#      green.
#
# Comments in `manifests/*` that say "Translated from /home/edgar/git/docker/..."
# are provenance, not bindings, and are intentionally not matched.
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
