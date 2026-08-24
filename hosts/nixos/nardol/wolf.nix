# Headless Games on Whales / Wolf host.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  imageConfigPolicy = import ./wolf/image-config-policy.nix {
    inherit lib pkgs;
  };
  containerGpuRuntime = import ./wolf/container-gpu-runtime.nix {
    inherit config lib pkgs;
    inherit (imageConfigPolicy)
      wolfContainerStatePath
      wolfHostStatePath
      wolfImage
      wolfImagePins
      ;
  };
in
lib.mkMerge [
  imageConfigPolicy.configuration
  containerGpuRuntime.configuration
  (import ./wolf/audio-vban-firewall.nix {
    inherit lib pkgs;
  })
  (import ./wolf/readiness-assertions.nix {
    inherit config lib pkgs;
    inherit (containerGpuRuntime)
      docker
      nvidiaAllocatorHostPath
      nvidiaEglVendorFile
      nvidiaSmi
      nvrtcLib
      renderNode
      ;
    inherit (imageConfigPolicy)
      validateWolfConfig
      wolfConfigData
      wolfConfigImageLines
      wolfConfigPolicy
      ;
  })
]
