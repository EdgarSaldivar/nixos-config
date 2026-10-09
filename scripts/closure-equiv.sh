#!/usr/bin/env bash
# Closure-equivalence harness.
#
# Every NixOS host here embeds the flake's git revision via
# `system.configurationRevision` (lib/mkHost.nix), which lands in the
# nixos-version package inside the system closure. So ANY commit changes
# toplevel.drvPath even when nothing functional changed -- the refactor signal
# drowns in the revision stamp.
#
# This pins configurationRevision to a constant on BOTH sides via
# extendModules, so a hash difference means a REAL difference.
#
# Usage: closure-equiv.sh <worktree-path>
set -euo pipefail
TREE="$(cd "$1" && pwd)"
failures=0

for host in nardol minas-tirith osgiliath pelargir; do
  printf '%-14s ' "$host"
  if ! nix eval --impure --raw --expr "
    let
      flake = builtins.getFlake \"path:$TREE\";
      pinned = flake.nixosConfigurations.$host.extendModules {
        modules = [ ({ lib, ... }: { system.configurationRevision = lib.mkForce \"PINNED-FOR-COMPARISON\"; }) ];
      };
    in pinned.config.system.build.toplevel.drvPath
  " 2>/dev/null | grep -oE '[a-z0-9]{32}'; then
    echo 'EVAL-FAILED'
    failures=1
  fi
  echo
done

printf '%-14s ' "dol-amroth"
# darwin does not go through mkNixos, so it carries no revision stamp.
if ! nix eval --raw "$TREE#darwinConfigurations.dol-amroth.config.system.build.toplevel.drvPath" 2>/dev/null \
  | grep -oE '[a-z0-9]{32}'; then
  echo 'EVAL-FAILED'
  failures=1
fi
echo

exit "$failures"
