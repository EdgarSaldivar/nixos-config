{
  lib,
  python3Packages,
}:
python3Packages.buildPythonApplication {
  pname = "terracompute-ops";
  version = "0.1.0";
  pyproject = true;
  src = lib.cleanSourceWith {
    src = ./.;
    filter = path: type:
      let
        name = baseNameOf path;
        relative = lib.removePrefix (toString ./. + "/") (toString path);
        top = builtins.head (lib.splitString "/" relative);
      in
        builtins.elem top [ "pyproject.toml" "src" "tests" "target" ]
        && lib.cleanSourceFilter path type
        && name != "__pycache__"
        && !(lib.hasSuffix ".pyc" name);
  };

  build-system = [ python3Packages.setuptools ];

  nativeCheckInputs = [ python3Packages.pyflakes ];
  postInstall = ''
    install -Dm755 target/terracompute-probe.py \
      "$out/libexec/terracompute-ops/terracompute-probe"
  '';
  checkPhase = ''
    runHook preCheck
    python -m unittest discover -s tests -v
    python tests/target_probe_test.py -v
    python -m pyflakes src tests
    python -m pyflakes target/terracompute-probe.py
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
