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
  # ⛔ Walk the WHOLE repository, not just hosts/.
  #
  # This was `../hosts`, and the traefik canary had already moved to experiments/ --
  # so its hostPath binding on the deleted directory was invisible here, and
  # `expected = [ ]` read as "zero bindings remain" while one remained. A ratchet
  # that reports all-clear because it stopped looking is worse than no ratchet.
  #
  # Widening is safe: the file-type filter below admits only code and config
  # (.nix/.yaml/.yaml.in/.sh/.py/.bats), so Markdown under docs/ -- which discusses
  # the old path historically and legitimately -- is never scanned, and comments
  # inside the files that ARE scanned are filtered out by isComment.
  hostsRoot = ../.;

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
          && (
            lib.hasSuffix ".nix" name
            || lib.hasSuffix ".yaml" name
            || lib.hasSuffix ".yaml.in" name
            # ⛔ .sh and .py are NOT optional extras. The shell extraction moved a
            # binding this check had been covering -- the shelfmark users.db dump
            # path -- out of backup-root-data.nix and into scripts/*.sh, a file type
            # this walk could not see. The same blind spot then covered scripts/*.py.
            #
            # This check's own header insists discovery must not depend on the author
            # already knowing the answer; restricting it to two extensions made it
            # depend on exactly that.
            || lib.hasSuffix ".sh" name
            || lib.hasSuffix ".py" name
            || lib.hasSuffix ".bats" name
          )
        then
          [ path ]
        else
          [ ];
    in
    lib.flatten (lib.mapAttrsToList go entries);

  relative = p: lib.removePrefix (toString ../. + "/") (toString p);

  # ⛔ Exclude THIS file. It defines `externalRoot`, so scanning the repository root
  # makes the check find its own definition and report it as a new binding -- which
  # is a self-reference, not a dependency. Everything else is in scope.
  sourceFiles = lib.filter (f: relative f != "checks/external-checkout-dependency.nix") (
    collect hostsRoot
  );

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

  # ⛔ THE SET IS EMPTY, and that is the point.
  #
  # This used to enumerate five bindings "as of 2026-08-16" with what had to happen
  # before each could go. All five are gone: immich and shelfmark moved to
  # /usr/local/etc/<service>, the hand-maintained traefik.yml was deleted, and the
  # traefik file provider moved to /usr/local/etc/traefik. The directory itself was
  # deleted the same day.
  #
  # The enumeration outlived them, describing entries that no longer exist as though
  # they were still pending. Cross-review flagged it on 2026-08-21.
  expected = lib.sort (a: b: a < b) [
  ];

  # ⛔ ANTI-VACUITY. `expected` is empty and the gate below throws only when `added`
  # is non-empty, so ANY discovery failure -- a wrong root, an extension list that
  # stops matching, a `collect` that returns [ ] -- yields `found = [ ]`, `added = [ ]`,
  # and this check PASSES having scanned nothing. That outcome is byte-identical to
  # genuine success, which is precisely the failure mode checks/python-lint.nix and
  # checks/workload-selectors.nix already guard against. It is worth more here than
  # anywhere else: this check is the ratchet that keeps the fleet OFF the deleted
  # Docker checkout, and a ratchet that silently stops engaging is worse than none.
  #
  # Two independent guards, because each catches what the other cannot:
  #   canary -- proves the walk actually REACHES a known file, so a wrong root fails
  #             even if some other tree happened to yield a plausible file count;
  #   floor  -- proves the walk did not stop after one directory, so a canary that
  #             happens to be found first cannot vouch for the rest of the tree.
  relativePaths = map relative sourceFiles;
  scanned = lib.length sourceFiles;
  minimumScanned = 150;
  canary = "flake.nix";

  # ⛔ Every admitted extension must actually be REPRESENTED in the scan.
  #
  # A canary plus a file-count floor is not enough: drop the `.py` matcher and the
  # root canary plus the other file types still clear the floor while every Python
  # binding becomes invisible. Per-extension coverage tests each matcher independently,
  # so losing ONE cannot hide behind the other five.
  #
  # ⚠️ If an extension legitimately has no files left, delete its matcher above AND
  # its entry here, in the same change. Do not silence this by loosening it.
  admittedExtensions = [
    ".nix"
    ".yaml"
    ".yaml.in"
    ".sh"
    ".py"
    ".bats"
  ];
  uncovered = lib.filter (ext: !(lib.any (pth: lib.hasSuffix ext pth) relativePaths)) admittedExtensions;

  added = lib.subtractLists expected found;
  removed = lib.subtractLists found expected;
  fmt = xs: lib.concatMapStringsSep "\n  " (x: x) xs;
in
if scanned < minimumScanned then
  throw ''
    VACUITY: external-checkout-dependency scanned only ${toString scanned} files;
    this repository's safety floor is ${toString minimumScanned}.

    The walk has stopped reaching a material part of the repository, so an empty
    `found` cannot be trusted. Fix discovery before lowering this floor. If a
    deliberate cleanup really removes that many admitted source files, lower the
    floor in the same reviewed change and record the new measured count here.
  ''
else if !(lib.elem canary relativePaths) then
  throw ''
    VACUITY: external-checkout-dependency scanned ${toString scanned} files but never
    reached `${canary}` at the repository root.

    Discovery is broken, so `found` is empty for a reason that has nothing to do with
    the fleet being clean -- and an empty `found` makes this check pass silently. Fix
    the walk (`collect` / the admitted extension list / `hostsRoot`) before trusting
    any result from it.
  ''
else if uncovered != [ ] then
  throw ''
    VACUITY: external-checkout-dependency scanned ${toString scanned} files but found
    NONE with these admitted extensions:
      ${fmt uncovered}

    Each extension in `admittedExtensions` had files in this repository when the guard
    was written, so an empty result means that matcher has stopped working and every
    binding in files of that type is now invisible to this check -- while the check
    still reports success.

    Fix the matcher in `collect`. If the extension is genuinely gone from the repo,
    remove it from BOTH `collect` and `admittedExtensions` in the same change.
  ''
else if added != [ ] then
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
    accurate. ⛔ Do NOT delete this check when `expected` is empty -- see the header:
    it is KEPT empty on purpose, as a regression guard for a path that no longer
    exists, so any new reference to it fails the build instead of failing on a host.
  ''
else
  pkgs.runCommand "external-checkout-dependency-ok" { } "touch $out"
