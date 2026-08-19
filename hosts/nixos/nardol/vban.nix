{
  lib,
  stdenv,
  fetchFromGitHub,
  cmake,
  libpulseaudio,
}:

stdenv.mkDerivation (finalAttrs: {
  pname = "vban";
  version = "2.2.0";

  src = fetchFromGitHub {
    owner = "quiniouben";
    repo = "vban";
    rev = "v${finalAttrs.version}";
    hash = "sha256-Zt+n2ESKH2Q10kS7GyKGfDEMfVkAQDzvjhseTO/dbxs=";
  };

  nativeBuildInputs = [ cmake ];
  buildInputs = [ libpulseaudio ];

  # Nardol only receives VBAN into Wolf's PulseAudio daemon. Avoid pulling
  # host ALSA/JACK policy into this single-purpose network receiver.
  cmakeFlags = [
    "-DWITH_ALSA=OFF"
    "-DWITH_JACK=OFF"
    "-DWITH_PULSEAUDIO=ON"
  ];

  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck

    set +e
    "$out/bin/vban_receptor" --help >vban-receptor-help.txt
    status=$?
    set -e
    test "$status" -eq 1
    grep -Fq -- "--backend=TYPE" vban-receptor-help.txt
    grep -Fq -- "--streamname=NAME" vban-receptor-help.txt

    runHook postInstallCheck
  '';

  meta = {
    description = "Command-line VBAN audio sender and receiver";
    homepage = "https://github.com/quiniouben/vban";
    license = lib.licenses.gpl3Plus;
    maintainers = [ ];
    platforms = lib.platforms.linux;
    mainProgram = "vban_receptor";
  };
})
