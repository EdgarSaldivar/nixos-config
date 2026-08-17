# Syntax and dead-name checking for every Python program in the repository.
#
# The shell programs got this on 2026-08-16 (`minas-shell-lint`) and immediately
# earned it. Python had `wolf-reconciler`'s pytest suite and nothing at all for the
# other four programs -- which between them provision a GHCR credential, assert the
# public ingress against a recorded baseline, and reconcile the k3s manifest set.
#
# ⛔ What this does NOT do: it does not run them. `pyflakes` catches the class that
# actually bites an unrun script -- a name that does not exist, an import that was
# removed, a variable assigned in a branch and read outside it -- which in a program
# invoked once a week by a timer would otherwise surface as a traceback nobody sees
# until they go looking for the report that was never written.
#
# The inventory is EXPLICIT, matching checks/default.nix. A `readDir` walk would
# silently start passing when a file is renamed out of the way, and the vacuity
# guard below would not notice because the count would still match itself.
{
  lib,
  pkgs,
  ...
}:
let
  programs = [
    ../hosts/nixos/minas-tirith/scripts/ingress-acceptance.py
    ../hosts/nixos/nardol/wolf-reconcile.py
    ../hosts/nixos/nardol/tests/test_wolf_reconcile.py
    ../hosts/nixos/pelargir/scripts/k3s-reconcile.py
    ../scripts/provision-ghcr-credential.py
  ];

  # pyflakes needs the third-party names these programs import to be resolvable,
  # otherwise every `import yaml` is reported as an unknown module rather than the
  # real finding it is looking for.
  python = pkgs.python3.withPackages (ps: [
    ps.pyflakes
    ps.pyyaml
    ps.tomlkit
    ps.pytest
  ]);
in
pkgs.runCommand "python-lint"
  {
    nativeBuildInputs = [ python ];
    files = lib.concatStringsSep " " (map toString programs);
    expected = toString (lib.length programs);
  }
  ''
    # ⛔ The sources live in the Nix store, which is READ-ONLY, and py_compile wants
    # to drop __pycache__ beside each file. Redirect the cache into the build's own
    # temp tree; without this every program "does not compile" for a reason that has
    # nothing to do with the program.
    export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"

    fail=0
    checked=0

    for f in $files; do
      name=$(basename "$f")
      if [ ! -f "$f" ]; then
        echo "FAIL: $name is in the inventory but does not exist" >&2
        fail=1
        continue
      fi

      # 1. it must parse.
      if ! python -m py_compile "$f"; then
        echo "FAIL: $name does not compile" >&2
        fail=1
      fi

      # 2. undefined names, unused imports, shadowed definitions.
      if ! python -m pyflakes "$f"; then
        echo "FAIL: $name has pyflakes findings" >&2
        fail=1
      fi

      checked=$((checked + 1))
    done

    # ANTI-VACUITY: a mistyped path or an empty list would lint nothing and pass.
    if [ "$checked" -ne "$expected" ]; then
      echo "VACUITY: linted $checked of $expected programs" >&2
      exit 1
    fi

    [ "$fail" -eq 0 ] || exit 1
    echo "python-lint: $checked programs clean"
    touch $out
  ''
