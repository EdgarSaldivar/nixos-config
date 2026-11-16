# Vendored VERBATIM from nixpkgs 745cee7c811a11977fe8733987cbecb69c4733d3,
# pkgs/os-specific/linux/gpu-unlock/firmware.nix ("linuxPackages.gpu-unlock: init at
# 2.0.0-unstable-2026-10-28", 2026-10-30). This flake's nixpkgs pin predates it.
# When the pin includes it, delete pkgs/gpu-unlock/ and use
# config.boot.kernelPackages.gpu-unlock instead (hosts/nixos/edoras/gpu-unlock.nix).

{
  lib,
  stdenvNoCC,
  fetchurl,
  python3,
  zstd,
  src,
  version,
}:

let
  # These assets are tied to gpu-unlock's loader, not the installed NVIDIA driver.
  nvidiaVersion = "610.43.03";
  nvidiaSource = fetchurl {
    url = "https://download.nvidia.com/XFree86/NVIDIA-kernel-module-source/NVIDIA-kernel-module-source-${nvidiaVersion}.tar.xz";
    hash = "sha256-fhGJI8eiPtw2EU1jJzpG4+BOmvmGlaQiA+esLf6fwdw=";
  };
  nvidiaInstaller = fetchurl {
    url = "https://download.nvidia.com/XFree86/Linux-x86_64/${nvidiaVersion}/NVIDIA-Linux-x86_64-${nvidiaVersion}-no-compat32.run";
    hash = "sha256-4IRFHbcwpt8p09dPZVdJxNI248lF99lLUgXoQxj+L5A=";
  };
in
stdenvNoCC.mkDerivation {
  pname = "gpu-unlock-firmware";
  inherit src version;

  nativeBuildInputs = [
    python3
    zstd
  ];

  buildPhase = ''
    runHook preBuild
    tar -xf ${nvidiaSource}
    python3 firmware/extract_gsp.py ${nvidiaInstaller} assets
    python3 firmware/extract_boot_assets.py NVIDIA-kernel-module-source-${nvidiaVersion} assets
    runHook postBuild
  '';

  makeFlags = [
    "ASSETS=assets"
    "FWDIR=$(out)/lib/firmware/gpu-unlock/${nvidiaVersion}"
    "LICENSEDIR=$(out)/share/licenses"
    "PKG=gpu-unlock-firmware"
  ];
  installTargets = [
    "install-firmware"
    "install-licenses"
  ];

  # Preserve the NVIDIA firmware byte-for-byte.
  dontFixup = true;

  meta = {
    description = "NVIDIA firmware assets for gpu-unlock";
    homepage = "https://github.com/Zagitta/gpu-unlock";
    license = lib.licenses.unfree;
    sourceProvenance = [ lib.sourceTypes.binaryFirmware ];
    teams = [ lib.teams.cuda ];
    maintainers = lib.teams.cuda.members;
    platforms = lib.platforms.all;
  };
}
