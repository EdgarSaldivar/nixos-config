# Every command in the extracted backup program must resolve to the package it was
# hard-coded to before the extraction.
#
# THE FAILURE THIS EXISTS TO CATCH
#
# Until 2026-08-16 `backup-root-data` invoked absolute store paths: there was no
# resolution step and no way to get the wrong binary. It is now a real `.sh` with
# bare names, resolved through `systemd.services.backup-root-data.path`. That trades
# an unreadable program for a program with a NEW failure mode -- PATH order.
#
# Adding one package to that list can silently shadow a command. Nothing would fail
# to build, no fixture would necessarily notice, and the backup would simply start
# calling a different binary at 03:00. This check is the thing that notices.
#
# TWO TIERS, because the packages are x86_64-linux
#
#   * everywhere: textual invariants -- the package set is exactly the eleven whose
#     store paths were previously interpolated, the mapping below is truthful, and
#     the program has not drifted back toward embedded store paths.
#   * x86_64-linux only: REAL resolution. Build the PATH and ask which directory
#     actually wins for each command. This is why the CI matrix has a Linux leg.
#
# The mapping is the contract. It was extracted mechanically from the 83
# interpolations that existed before the change, not written from memory.
{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:
let
  minas = nixosConfigurations.minas-tirith.config;

  program = ../hosts/nixos/minas-tirith/scripts/backup-root-data.sh;

  # command -> the pkgs attribute whose store path it used to be interpolated from
  expectedOwner = {
    age = "age";
    basename = "coreutils";
    chown = "coreutils";
    dirname = "coreutils";
    install = "coreutils";
    stat = "coreutils";
    docker = "docker";
    grep = "gnugrep";
    sed = "gnused";
    gzip = "gzip";
    k3s = "k3s";
    rsync = "rsync";
    sqlite3 = "sqlite";
    findmnt = "util-linux";
    flock = "util-linux";
    setpriv = "util-linux";
    zfs = "zfs";
  };

  # ⚠️ `systemd.services.<n>.path` is the MERGED list, not the one written in the
  # module: NixOS appends these five to every service. They were already on the unit
  # PATH before the extraction (they are the whole of the old PATH), so they are part
  # of the contract, not noise -- and `coreutils`, `gnugrep` and `gnused` therefore
  # appear twice in the raw list.
  #
  # Compared as a SET. Multiplicity is an artefact of that merge and says nothing;
  # ORDER is what decides resolution, and `minas-unit-contract` pins it exactly.
  nixosServiceDefaults = [
    "coreutils"
    "findutils"
    "gnugrep"
    "gnused"
    "systemd"
  ];

  expectedPackages = lib.unique (lib.attrValues expectedOwner ++ nixosServiceDefaults);

  # `pname` is what survives into a store path's name; `zfs` builds as `zfs-user`,
  # so compare on the derivation's own pname rather than on the attribute name.
  actualPackages = lib.unique (
    map (p: p.pname or (lib.getName p)) minas.systemd.services.backup-root-data.path
  );

  realResolution = pkgs.stdenv.hostPlatform.system == "x86_64-linux";

  pathFor = lib.makeBinPath minas.systemd.services.backup-root-data.path;
in
pkgs.runCommand "minas-command-resolution"
  {
    inherit pathFor;
    commands = lib.concatStringsSep " " (lib.attrNames expectedOwner);
    owners = lib.concatStringsSep " " (lib.mapAttrsToList (c: o: "${c}:${o}") expectedOwner);
    expectedPkgs = lib.concatStringsSep " " (lib.sort (a: b: a < b) expectedPackages);
    actualPkgs = lib.concatStringsSep " " (lib.sort (a: b: a < b) actualPackages);
    inherit program;
  }
  ''
    fail=0

    # ---- 1. the program must stay extracted -----------------------------------
    if grep -q '/nix/store/' $program; then
      echo "FAIL: embedded store paths reappeared in backup-root-data.sh:" >&2
      grep -n '/nix/store/' $program >&2
      fail=1
    fi

    # ---- 2. the mapping must be truthful --------------------------------------
    # Every command claimed below must actually be invoked by the program. A stale
    # entry would make this check quietly weaker than it looks.
    # Comments are stripped first. Review pointed out that the bare regex is
    # satisfied by a mention in prose -- and this program is heavily commented, so
    # "the mapping is truthful" would have been close to unfalsifiable.
    sed -E 's/(^|[[:space:]])#.*$/\1/' $program > program-code.sh

    for c in $commands; do
      if ! grep -qE "(^|[^A-Za-z0-9_./-])$c([^A-Za-z0-9_-]|$)" program-code.sh; then
        echo "FAIL: mapping claims '$c' is used, but it does not appear in the program." >&2
        fail=1
      fi
    done

    # ---- 3. the package set must be exactly the expected one -------------------
    # Sorted comparison, not a count: swapping one package for another must fail.
    if [ "$expectedPkgs" != "$actualPkgs" ]; then
      echo "FAIL: systemd path package set changed." >&2
      echo "  expected: $expectedPkgs" >&2
      echo "  actual  : $actualPkgs" >&2
      fail=1
    fi

    # ---- 4. REAL resolution, where the packages can be built ------------------
    ${
      if realResolution then
        ''
          echo "resolving against the real PATH (x86_64-linux)"
          resolved=0
          for pair in $owners; do
            c="''${pair%%:*}"; want="''${pair##*:}"
            got="$(PATH="$pathFor" command -v "$c" 2>/dev/null || true)"
            if [ -z "$got" ]; then
              echo "FAIL: '$c' does not resolve on the unit PATH at all." >&2
              fail=1
              continue
            fi
            # /nix/store/<hash>-<pname>-<version>/bin/<cmd>
            owner="$(echo "$got" | sed -E 's#^/nix/store/[a-z0-9]{32}-##; s#/bin/.*##; s#-[0-9].*$##')"
            if [ "$owner" != "$want" ] && [ "$owner" != "$want-user" ]; then
              echo "FAIL: '$c' resolves to '$owner', expected '$want' ($got)" >&2
              fail=1
            fi
            resolved=$((resolved + 1))
          done
          if [ "$resolved" -lt 17 ]; then
            echo "VACUITY: only $resolved commands were resolved, expected 17." >&2
            fail=1
          fi
          echo "resolved $resolved commands"
        ''
      else
        ''
          echo "skipping real resolution: packages are x86_64-linux, this is ${pkgs.stdenv.hostPlatform.system}"
          echo "textual invariants were still checked; the Linux CI leg does the rest"
        ''
    }

    [ "$fail" -eq 0 ] || exit 1
    touch $out
  ''
