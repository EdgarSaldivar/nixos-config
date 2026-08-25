{
  lib,
  python3,
  runCommand,
}:

let
  # Explicit by design: adding a module must also add it to the installed closure.
  moduleSources = [
    {
      source = ./ingress_acceptance/__init__.py;
      target = "__init__.py";
    }
    {
      source = ./ingress_acceptance/certificate.py;
      target = "certificate.py";
    }
    {
      source = ./ingress_acceptance/cli.py;
      target = "cli.py";
    }
    {
      source = ./ingress_acceptance/evaluation.py;
      target = "evaluation.py";
    }
    {
      source = ./ingress_acceptance/models.py;
      target = "models.py";
    }
    {
      source = ./ingress_acceptance/network.py;
      target = "network.py";
    }
    {
      source = ./ingress_acceptance/rendering.py;
      target = "rendering.py";
    }
  ];
in
runCommand "ingress-acceptance"
  {
    nativeBuildInputs = [ python3 ];
  }
  ''
    mkdir -p "$out/bin/ingress_acceptance"
    install -m 0755 ${./ingress-acceptance.py} "$out/bin/ingress-acceptance"
    substituteInPlace "$out/bin/ingress-acceptance" \
      --replace-fail '#!/usr/bin/env python3' '#!${python3}/bin/python3'
    chmod 0555 "$out/bin/ingress-acceptance"

    ${lib.concatMapStringsSep "\n" (module: ''
      install -m 0444 ${module.source} "$out/bin/ingress_acceptance/${module.target}"
    '') moduleSources}

    export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
    "$out/bin/ingress-acceptance" --selftest
  ''
