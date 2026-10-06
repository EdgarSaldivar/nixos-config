{
  lib,
  python3Packages,
  ffmpeg,
  makeWrapper,
}:
python3Packages.buildPythonApplication {
  pname = "media-optimizer";
  version = "0.1.0";
  pyproject = true;
  src = ./.;
  build-system = [ python3Packages.setuptools ];
  nativeBuildInputs = [ makeWrapper ];
  nativeCheckInputs = [ python3Packages.pyflakes ];
  checkPhase = ''
    runHook preCheck
    python -m unittest discover -s tests -v
    python -m pyflakes src tests
    runHook postCheck
  '';
  postFixup = ''
    wrapProgram "$out/bin/media-optimizer" --prefix PATH : ${lib.makeBinPath [ ffmpeg ]}
  '';
  pythonImportsCheck = [ "media_optimizer.cli" ];
  meta = {
    description = "Journaled public-media replacement download optimizer";
    mainProgram = "media-optimizer";
    license = lib.licenses.mit;
    platforms = lib.platforms.unix;
  };
}
