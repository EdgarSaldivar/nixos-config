{ lib, pkgs, ... }:
let
  # Temporarily set this true for the first activation on a Mac without a Linux
  # builder. The pinned nix-darwin docs require its binary-cached default guest
  # to be started before customizing the guest closure; otherwise building that
  # closure already requires the aarch64-linux builder being created. The
  # committed steady state is false so the declared resource limits really are
  # active. README.md documents the two-stage bootstrap and restoring this value
  # before the second activation.
  bootstrapLinuxBuilder = false;
in
{
  environment.systemPackages = with pkgs; [
    vim
    wget
  ];

  networking.hostName = "dol-amroth";
  users.users.edgar.home = "/Users/edgar";

  nixpkgs.config.allowUnfree = true;
  time.timeZone = "America/Los_Angeles";
  system.stateVersion = 5;

  nix = {
    settings = {
      experimental-features = "nix-command flakes";
      allowed-users = [ "*" ];
      # nixpkgs' darwin-builder documentation requires the invoking user to be
      # trusted; without this, the declared builder exists but Edgar cannot use
      # it for remote fleet closures.
      trusted-users = [
        "root"
        "edgar"
      ];
    };

    # Pinned nix-darwin c3e90c8 registers this VM in nix.buildMachines, writes
    # its localhost SSH configuration, enables distributed builds, and lets the
    # guest use substitutes. Persistent storage avoids rebuilding fleet
    # closures after each Mac restart.
    linux-builder = {
      enable = true;
      ephemeral = false;
      systems = [ "aarch64-linux" ];
    }
    // lib.optionalAttrs (!bootstrapLinuxBuilder) {
      config.virtualisation = {
        cores = 6;
        darwin-builder = {
          memorySize = 8 * 1024;
          diskSize = 100 * 1024;
        };
      };
    };
  };
}
