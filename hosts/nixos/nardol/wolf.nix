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
  {
    environment.etc."nardol/steamwebhelper-runtime/_v2-entry-point" = {
      source = ./wolf/steamwebhelper-runtime.sh;
      mode = "0555";
    };
    environment.etc."nardol/sway-game-focus.conf" = {
      source = ./wolf/sway-game-focus.conf;
      mode = "0444";
    };
  }
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
