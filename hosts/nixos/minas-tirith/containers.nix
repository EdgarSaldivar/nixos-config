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
      #
      # NOTE the key is PLURAL. The old openSUSE /etc/docker/daemon.json used
      # `default-address-pool` (singular) — the CLI flag name, not the daemon.json
      # key — so dockerd silently ignored it and fell back to its built-in
      # 172.17–172.31 pool. Verified on the live host: every network was a /16,
      # not the configured /24. The pin only *appeared* to work because Docker's
      # defaults happen to sit inside 172.16/12.
      default-address-pools = [
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
  # Docker MUST NOT start before the ZFS pools are mounted.
  # ---------------------------------------------------------------------------
  # Four of the six compose projects bind-mount paths under /storage and
  # /storage2. All 39 containers carry restart policies, so on every boot they
  # race the pool import. If Docker wins, it helpfully CREATES EMPTY DIRECTORIES
  # at those mountpoints — which then blocks the ZFS mount, and the services come
  # up pointed at empty storage. Recovering means stopping everything, deleting
  # the stray directories, and re-importing.
  #
  # This is a boot-time race that would bite silently and repeatedly, so the
  # ordering is encoded here rather than left to the restore runbook.
  systemd.services.docker = {
    after = [
      "zfs-import.target"
      "zfs-mount.service"
      "storage.mount"
      "storage2.mount"
    ];
    requires = [ "zfs-mount.service" ];
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
