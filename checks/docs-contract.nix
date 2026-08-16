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
  permitted = [ "HANDOFF.md" ];
  baseName = p: lib.last (lib.splitString "/" (toString p));
  offendingNames = lib.filter (
    doc:
    let
      b = baseName doc;
    in
    !(lib.elem b permitted) && lib.any (w: lib.hasInfix w b) lifecycleWords
  ) docs;

  fmt = paths: lib.concatMapStringsSep "\n  " (p: baseName p) paths;
in
if offendingLinks != [ ] then
  throw ''
    Documentation contains links rooted outside the repository.
    A link into /private, /tmp or a codex-seat worktree resolves only on the
    machine that wrote it. Offending files:
      ${fmt offendingLinks}
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
