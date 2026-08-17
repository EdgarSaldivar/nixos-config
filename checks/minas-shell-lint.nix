# ShellCheck and a parse gate on the extracted shell programs.
#
# WHY THIS IS THE POINT OF THE EXTRACTION
#
# ROADMAP item 2's complaint was that these programs were "invisible to ShellCheck".
# Extracting them to real `.sh` files does not by itself fix that -- it only makes
# it POSSIBLE. This check is the part that actually makes it true, and without it
# the extraction would have traded a readable-but-unlinted program for a
# readable-but-unlinted program.
#
# WHAT CROSS-REVIEW SAID, AND WHY THIS EXISTS IN THIS FORM
#
# `minas-unit-contract` normalises the ExecStart store path away, so it cannot see
# the program body AT ALL -- any body change simply produces a different path that
# is normalised out. Independent review made that concrete: deleting a line
# continuation from the rsync invocation passes the unit contract, passes the
# command-resolution check, contains no store path, keeps every mapped command
# present, and still leaves rsync with no source or destination at 03:00. The
# fixtures do not execute that block.
#
# ShellCheck plus `bash -n` is what catches that class. It is not a completeness
# proof either -- nothing short of executing the program is -- but it closes the
# specific hole where a corrupted body sails through every structural check.
#
# The Nix-residue guards below close the other named hole: a surviving
# `${config...}` interpolation contains no `/nix/store/` and no `${pkgs.`, so it
# passes both existing checks and then fails at runtime as a bash bad substitution.
{
  lib,
  pkgs,
  ...
}:
let
  programs = {
    backup-root-data = ../hosts/nixos/minas-tirith/scripts/backup-root-data.sh;
  };
in
pkgs.runCommand "minas-shell-lint"
  {
    nativeBuildInputs = [
      pkgs.shellcheck
      pkgs.bash
    ];
    names = lib.concatStringsSep " " (lib.attrNames programs);
    paths = lib.concatStringsSep " " (lib.attrValues programs);
  }
  ''
    fail=0
    checked=0

    set -- $paths
    for name in $names; do
      src="$1"; shift
      echo "=== $name ==="

      # 1. it must parse. Catches unbalanced quotes, broken here-docs and the
      #    continuation-backslash class that review raised.
      if ! bash -n "$src"; then
        echo "FAIL: $name does not parse" >&2
        fail=1
      fi

      # 2. ShellCheck. `-s bash` because the file has no shebang: it is fed to
      #    NixOS's `script =`, which supplies one.
      if ! shellcheck -s bash "$src"; then
        echo "FAIL: $name has ShellCheck findings" >&2
        fail=1
      fi

      # 3. No Nix residue. A surviving interpolation is invisible to every other
      #    check here and becomes a runtime bad substitution.
      if grep -nE '\$\{(pkgs|config|lib|inputs)\.' "$src"; then
        echo "FAIL: $name contains an unexpanded Nix interpolation" >&2
        fail=1
      fi
      if grep -n '/nix/store/' "$src"; then
        echo "FAIL: $name contains an embedded store path" >&2
        fail=1
      fi
      # (No check for a doubled single-quote here on purpose: once the program is a
      # real .sh, that sequence is bash's ordinary empty-string idiom. Forbidding it
      # would ban legitimate shell to guard against a Nix escape that cannot survive
      # -- the extractor aborts if one does, and `bash -n` would reject it anyway.)

      # 4. Whitespace hygiene. Review flagged CRLF and trailing whitespace as
      #    extraction-specific corruption that changes behaviour invisibly:
      #    a continuation backslash followed by a space stops continuing.
      if grep -nP '\r' "$src" 2>/dev/null; then
        echo "FAIL: $name contains CR characters" >&2
        fail=1
      fi
      if grep -nE '[[:space:]]+$' "$src"; then
        echo "FAIL: $name has trailing whitespace" >&2
        fail=1
      fi
      if grep -nE '\\[[:space:]]+$' "$src"; then
        echo "FAIL: $name has a line continuation followed by whitespace" >&2
        fail=1
      fi

      checked=$((checked + 1))
    done

    # ANTI-VACUITY: an empty or mis-split program list would pass everything above.
    if [ "$checked" -ne 1 ]; then
      echo "VACUITY: expected to lint 1 program, linted $checked" >&2
      exit 1
    fi

    [ "$fail" -eq 0 ] || exit 1
    touch $out
  ''
