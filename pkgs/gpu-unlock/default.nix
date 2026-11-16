# Vendored VERBATIM from nixpkgs 745cee7c811a11977fe8733987cbecb69c4733d3,
# pkgs/os-specific/linux/gpu-unlock/default.nix ("linuxPackages.gpu-unlock: init at
# 2.0.0-unstable-2026-10-28", 2026-10-30). This flake's nixpkgs pin predates it.
# When the pin includes it, delete pkgs/gpu-unlock/ and use
# config.boot.kernelPackages.gpu-unlock instead (hosts/nixos/edoras/gpu-unlock.nix).

{
  lib,
  stdenv,
  callPackage,
  fetchFromGitHub,
  kernel,
  kernelModuleMakeFlags,
}:

stdenv.mkDerivation (finalAttrs: {
  pname = "gpu-unlock";
  version = "2.0.0-unstable-2026-10-28";

  src = fetchFromGitHub {
    owner = "Zagitta";
    repo = "gpu-unlock";
    rev = "7eb51bccae0a0b7e599e084d971170e9a1e82d0c";
    hash = "sha256-1KD/J1S1uyI5GGoTwQ1cU/sXeEKlG54exSvi6U+j73M=";
  };

  nativeBuildInputs = kernel.moduleBuildDependencies;

  hardeningDisable = [ "pic" ];
  enableParallelBuilding = true;

  setSourceRoot = ''
    export sourceRoot=$(pwd)/${finalAttrs.src.name}
  '';

  makeFlags = kernelModuleMakeFlags ++ [
    "-C"
    "${kernel.dev}/lib/modules/${kernel.modDirVersion}/build"
    "M=$(sourceRoot)/kernel/gpu_unlock"
  ];

  buildFlags = [ "modules" ];
  installFlags = [ "INSTALL_MOD_PATH=${placeholder "out"}" ];
  installTargets = [ "modules_install" ];

  postInstall = ''
    install -Dm644 -t "$out/share/licenses/gpu-unlock" licenses/*.txt
  '';

  # modules_install already strips, signs and compresses the module.
  dontStrip = true;
  dontPatchELF = true;

  passthru.firmware = callPackage ./firmware.nix {
    inherit (finalAttrs) src version;
  };

  meta = {
    description = "Kernel module to unlock tensor core throughput on supported NVIDIA GeForce GPUs";
    homepage = "https://github.com/Zagitta/gpu-unlock";
    license = lib.licenses.gpl2Only;
    teams = [ lib.teams.cuda ];
    maintainers = lib.teams.cuda.members;
    platforms = [ "x86_64-linux" ];
  };
})
