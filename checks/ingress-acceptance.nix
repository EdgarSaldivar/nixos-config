# Offline behavioral characterization for the modular ingress acceptance tool.
{
  lib,
  pkgs,
  ...
}:
let
  # Explicit inventory prevents a renamed or omitted module from disappearing from
  # the test tree while the check continues to report success.
  sources = [
    {
      source = ../hosts/nixos/minas-tirith/scripts/ingress-acceptance.py;
      target = "ingress-acceptance.py";
    }
    {
      source = ../hosts/nixos/minas-tirith/scripts/ingress_acceptance/__init__.py;
      target = "ingress_acceptance/__init__.py";
    }
    {
      source = ../hosts/nixos/minas-tirith/scripts/ingress_acceptance/certificate.py;
      target = "ingress_acceptance/certificate.py";
    }
    {
      source = ../hosts/nixos/minas-tirith/scripts/ingress_acceptance/cli.py;
      target = "ingress_acceptance/cli.py";
    }
    {
      source = ../hosts/nixos/minas-tirith/scripts/ingress_acceptance/evaluation.py;
      target = "ingress_acceptance/evaluation.py";
    }
    {
      source = ../hosts/nixos/minas-tirith/scripts/ingress_acceptance/models.py;
      target = "ingress_acceptance/models.py";
    }
    {
      source = ../hosts/nixos/minas-tirith/scripts/ingress_acceptance/network.py;
      target = "ingress_acceptance/network.py";
    }
    {
      source = ../hosts/nixos/minas-tirith/scripts/ingress_acceptance/rendering.py;
      target = "ingress_acceptance/rendering.py";
    }
    {
      source = ../hosts/nixos/minas-tirith/scripts/tests/test_ingress_acceptance.py;
      target = "tests/test_ingress_acceptance.py";
    }
  ];
in
pkgs.runCommand "ingress-acceptance-tests"
  {
    nativeBuildInputs = [ pkgs.python3 ];
    expectedSources = toString (lib.length sources);
    expectedTests = "7";
  }
  ''
    work="$TMPDIR/ingress-acceptance"
    mkdir -p "$work/ingress_acceptance" "$work/tests"
    copied=0
    ${lib.concatMapStringsSep "\n" (file: ''
      cp ${file.source} "$work/${file.target}"
      copied=$((copied + 1))
    '') sources}

    if [ "$copied" -ne "$expectedSources" ]; then
      echo "VACUITY: copied $copied of $expectedSources ingress sources" >&2
      exit 1
    fi

    export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
    testOutput=$(python "$work/tests/test_ingress_acceptance.py" 2>&1) || {
      echo "$testOutput" >&2
      exit 1
    }
    echo "$testOutput"
    if ! echo "$testOutput" | grep -Fq "Ran $expectedTests tests"; then
      echo "VACUITY: expected $expectedTests ingress tests to run" >&2
      exit 1
    fi

    selftestOutput=$(python "$work/ingress-acceptance.py" --selftest)
    if [ "$selftestOutput" != "SELFTEST OK" ]; then
      echo "FAIL: ingress selftest output changed: $selftestOutput" >&2
      exit 1
    fi

    echo "ingress-acceptance: $expectedTests tests and $copied sources checked"
    touch "$out"
  ''
