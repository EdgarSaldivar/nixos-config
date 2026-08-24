# Documentation contract.
#
# This repository has drifted before, badly enough to need a dedicated cleanup:
# README claimed three invariants when there were fourteen, a runbook told the
# operator to run a rollback command that cannot work on a channel-less flake, and
# sixteen live manifests pointed at a section of a document that was about to be
# deleted. Every one of those was discoverable by reading. None was discovered by
# reading, because nothing failed.
#
# So the parts that CAN be mechanically checked are checked here. This does not
# make documentation correct — nothing can — but it makes a specific, recurring
# class of wrongness impossible to commit:
#
#   1. a reference from code or docs to a document that does not exist
#   2. a link rooted in somebody's temporary worktree
#   3. lifecycle artifacts (plans, reviews, handoffs, ledgers) accumulating in HEAD
#      beside live procedure, where a reader cannot tell which is which
#
# Deliberately NOT checked: whether prose is true. That needs a human, and
# pretending otherwise would be worse than not checking at all.
{
  lib,
  pkgs,
  ...
}:
let
  root = ../.;

  # Every tracked Markdown file, plus the sources most likely to cite one.
  # `builtins.readDir` is used for DISCOVERY here rather than for wiring modules —
  # a file this misses is simply unchecked, not silently unbuilt.
  collect =
    dir: pred:
    let
      entries = builtins.readDir dir;
      go =
        name: type:
        let
          path = dir + "/${name}";
        in
        if type == "directory" && name != ".git" && name != "result" then
          collect path pred
        else if type == "regular" && pred name then
          [ path ]
        else
          [ ];
    in
    lib.flatten (lib.mapAttrsToList go entries);

  baseName = p: lib.last (lib.splitString "/" (toString p));
  isDoc = name: lib.hasSuffix ".md" name;
  docs = collect root isDoc;

  # ── 1. no links into a temporary worktree ────────────────────────────────────
  # K3S-PHASE1-REVIEW.md was committed containing dozens of links rooted at
  # /private/var/.../codex-seat-wt-.../run-1786053455-56756/, which were never
  # resolvable outside the worker checkout that produced them.
  forbiddenRoots = [
    "](/private/"
    "](/tmp/"
    "](file://"
    "codex-seat-wt-"
  ];
  offendingLinks = lib.filter (
    doc:
    let
      text = builtins.readFile doc;
    in
    lib.any (frag: lib.hasInfix frag text) forbiddenRoots
  ) docs;

  # ── 1b. every relative Markdown link must RESOLVE ────────────────────────────
  # The header above has always promised this; an earlier version only blacklisted
  # substrings, which is not the same thing and let a genuinely broken link ship.
  # A link resolves relative to the DIRECTORY OF THE FILE CONTAINING IT, which is
  # exactly the trap that produced that bug: a repository-root-relative path is
  # correct in prose and wrong inside `](...)`.
  repoRoot = toString ../.;
  dirOf =
    p:
    let
      parts = lib.splitString "/" (toString p);
    in
    lib.concatStringsSep "/" (lib.init parts);

  linksIn =
    doc:
    let
      text = builtins.readFile doc;
      # No regex: Nix uses POSIX ERE and the bracket escaping is a trap. Split on
      # the literal "](", take each fragment up to the first ")", and keep the ones
      # that look like a relative .md path.
      afterOpen = lib.drop 1 (lib.splitString "](" text);
      targets = map (frag: lib.head (lib.splitString ")" frag)) afterOpen;
      relevant = lib.filter (
        # ⚠️ Strip a `#fragment` BEFORE deciding whether this is a Markdown link.
        # Filtering on `hasSuffix ".md"` alone silently skipped every anchored link
        # (`](foo.md#section)`), which is a common form -- so the header's promise
        # that "every relative Markdown link must RESOLVE" was not being kept for
        # exactly the links most likely to rot when a heading is renamed.
        t:
        let
          path = lib.head (lib.splitString "#" t);
        in
        lib.hasSuffix ".md" path && !(lib.hasInfix "://" t) && !(lib.hasPrefix "#" t) && t != ""
      ) targets;
      # ⛔ Strip the `#fragment` HERE TOO, not only in the filter above.
      #
      # The filter strips it to DECIDE whether a target is a Markdown link; resolving
      # the original target then probes a literal `foo.md#section` path, which never
      # exists on disk. Every anchored link would therefore be reported broken --
      # punishing exactly the link form the filter was widened to support, and making
      # the check actively worse than when it skipped them.
      resolve =
        target:
        let
          path = lib.head (lib.splitString "#" target);
        in
        if lib.hasPrefix "/" path then repoRoot + path else "${dirOf doc}/${path}";
    in
    lib.filter (t: !(builtins.pathExists (resolve t))) relevant;

  brokenLinks = lib.flatten (map (doc: map (t: "${baseName doc} -> ${t}") (linksIn doc)) docs);

  # ── 2. no lifecycle artifacts in HEAD ────────────────────────────────────────
  # A plan that has been executed, or a review of a plan that has been executed, is
  # history. Beside a live runbook it is a hazard. Git history and the
  # pre-doc-cleanup-2026-08 tag hold the archive.
  lifecycleWords = [
    "PLAN"
    "REVIEW"
    "HANDOFF"
    "LEDGER"
    "BASELINE"
    "FOLLOWUPS"
    "BLOCKER"
  ];
  # HANDOFF.md at the repository root is the one permitted exception: it is the
  # live session-to-session handoff, not an archived artifact.
  # ⛔ Root-relative, not basename. An earlier version matched the BASENAME, which
  # would have exempted a HANDOFF.md anywhere in the tree — including one dropped
  # into docs/runbooks/, which is exactly the placement the rule exists to stop.
  permittedPaths = [ (toString ../HANDOFF.md) ];
  offendingNames = lib.filter (
    doc:
    let
      b = baseName doc;
    in
    !(lib.elem (toString doc) permittedPaths) && lib.any (w: lib.hasInfix w b) lifecycleWords
  ) docs;

  fmt = paths: lib.concatMapStringsSep "\n  " (p: baseName p) paths;
  fmtStr = xs: lib.concatMapStringsSep "\n  " (x: x) xs;
  # ⛔ Prose that states a COUNT must be checkable, or it drifts silently.
  #
  # README.md said "seventeen invariants" while checks/ declared 24. Nothing could
  # catch that: it is documentation-only, so no build breaks and no closure changes --
  # exactly the drift class this restructure exists to remove, sitting in the file
  # that advertises the removal.
  checkCount = lib.length (
    lib.filter (n: n != "default.nix" && lib.hasSuffix ".nix" n) (
      lib.attrNames (builtins.readDir ../checks)
    )
  );

  countClaims =
    let
      claimed = builtins.match ".*enforces ([0-9]+) invariants.*" (builtins.readFile ../README.md);
    in
    if claimed == null then
      [ "README.md no longer states a check count; the guard has stopped guarding" ]
    else if lib.head claimed == toString checkCount then
      [ ]
    else
      [ "README.md claims ${lib.head claimed} invariants, checks/ has ${toString checkCount}" ];

in
# ⛔ Guard the vacuous pass. If discovery ever returns nothing — a readDir change, a
# moved root — every branch below is trivially satisfied and this check would report
# success while protecting nothing. The repository always has documentation.
if docs == [ ] then
  throw "docs-contract found no Markdown at all; discovery is broken and this check would pass vacuously"
else if offendingLinks != [ ] then
  throw ''
    Documentation contains links rooted outside the repository.
    A link into /private, /tmp or a codex-seat worktree resolves only on the
    machine that wrote it. Offending files:
      ${fmt offendingLinks}
  ''
else if brokenLinks != [ ] then
  throw ''
    Documentation contains Markdown links that do not resolve.
    A link resolves relative to the directory of the file containing it, so a
    repository-root-relative path is correct in prose and WRONG inside `](...)`.
    Broken links:
      ${fmtStr brokenLinks}
  ''
else if countClaims != [ ] then
  throw ''
    Documentation states a check count that no longer matches checks/.
    A number in prose is a claim like any other, and this one had drifted by seven.
      ${fmtStr countClaims}
  ''
else if offendingNames != [ ] then
  throw ''
    Lifecycle artifacts must not live in HEAD beside operational documentation.
    Plans, reviews, handoffs, ledgers and captured baselines are history: they
    belong in git history and behind the pre-doc-cleanup-2026-08 tag, not next to
    a runbook somebody will follow at 2am. Offending files:
      ${fmt offendingNames}
  ''
else
  pkgs.runCommand "docs-contract-ok" { } "touch $out"
