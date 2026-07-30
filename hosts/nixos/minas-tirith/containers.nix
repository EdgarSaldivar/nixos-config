# minas-tirith (raz-server) — Docker + NVIDIA for the container stack.
#
# 39 containers across five docker-compose projects, restored from
# ~/git/docker/{infra,media,cloud,books,immich}. Deliberately NOT rewritten as
# `virtualisation.oci-containers` for the migration: porting 39 services to Nix
# at the same time as changing distro means a breakage can't be attributed. Get
# the compose stacks running as-is first, then port them one project at a time
# if desired.
{ config, pkgs, ... }:
{
  virtualisation.docker = {
    enable = true;
    # Compose v2 as a docker plugin (`docker compose ...`), matching how the
    # existing stacks are invoked.
    daemon.settings = {
      # ⚠️  DO NOT REMOVE. Docker once auto-assigned 192.168.0.0/20 here, which
      # collided with the WireGuard client subnet (192.168.4.0/24) and
      # blackholed VPN return traffic. Pinning the pool away from 192.168.x and
      # 10.x is load-bearing for remote access to this machine.
      default-address-pool = [
        {
          base = "172.16.0.0/12";
          size = 24;
        }
      ];

      # NOTE the deliberate omission: the old daemon.json set
      #     "default-runtime": "nvidia"
      # which meant a broken nvidia-container-toolkit took down ALL 39
      # containers, not just the GPU one. Only the GPU container should request
      # the nvidia runtime (compose: `runtime: nvidia` or `deploy.resources.
      # reservations.devices`). Leaving the default as runc shrinks the blast
      # radius enormously.
      log-driver = "json-file";
      log-opts = {
        # The old host had no rotation and accumulated 662 MB of container logs
        # on a wearing SSD.
        max-size = "10m";
        max-file = "3";
      };
    };
    autoPrune = {
      enable = true;
      dates = "weekly";
      flags = [ "--filter=until=168h" ];
    };
  };

  # ---------------------------------------------------------------------------
  # NVIDIA — RTX 2080 (Turing). Used by the Triton model-service container and
  # by Plex/Jellyfin for transcode.
  # ---------------------------------------------------------------------------
  hardware.graphics.enable = true;

  hardware.nvidia = {
    # Turing is well served by the production branch; `open` drivers do not
    # support Turing.
    package = config.boot.kernelPackages.nvidiaPackages.production;
    open = false;
    modesetting.enable = true;
    nvidiaSettings = false; # headless server
    powerManagement.enable = false;
  };

  # Modern replacement for the removed `virtualisation.docker.enableNvidia`.
  # Provides the nvidia container runtime + CDI so containers can request the GPU.
  hardware.nvidia-container-toolkit.enable = true;

  # Headless: no X/Wayland, but the kernel module must still load.
  services.xserver.videoDrivers = [ "nvidia" ];

  # Jellyfin transcodes to /dev/shm on the old host (RAM, not the SSD) — worth
  # preserving, it kept transcode writes off a drive at 33% wear.
  boot.tmp.useTmpfs = false; # /tmp on disk; /dev/shm is separate and default-sized
}
