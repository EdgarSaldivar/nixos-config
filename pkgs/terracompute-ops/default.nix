{
  lib,
  python3Packages,
}:
python3Packages.buildPythonApplication {
  pname = "terracompute-ops";
  version = "0.1.0";
  pyproject = true;
  src = ./.;

  build-system = [ python3Packages.setuptools ];

  nativeCheckInputs = [ python3Packages.pyflakes ];
  checkPhase = ''
    runHook preCheck
    python -m unittest discover -s tests -v
    python -m pyflakes src tests
    runHook postCheck
  '';

  pythonImportsCheck = [ "terracompute_ops" ];

  meta = {
    description = "Observation-only terracompute incident supervisor";
    mainProgram = "terracompute-ops";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
  };
}
