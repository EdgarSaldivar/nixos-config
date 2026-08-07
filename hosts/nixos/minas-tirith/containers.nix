# minas-tirith (raz-server) — Docker + NVIDIA for the container stack.
#
# 39 containers across five docker-compose projects, restored from
# ~/git/docker/{infra,media,cloud,books,immich}. Deliberately NOT rewritten as
# `virtualisation.oci-containers` for the migration: porting 39 services to Nix
# at the same time as changing distro means a breakage can't be attributed. Get
# the compose stacks running as-is first, then port them one project at a time
# if desired.
{
  config,
  lib,
  pkgs,
  ...
}:
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
  #
  # An earlier attempt ordered docker After= storage.mount/storage2.mount and
  # Requires= zfs-mount.service. That was decorative and wrong on three counts:
  #   - `storage.mount` / `storage2.mount` DO NOT EXIST. These pools use native
  #     ZFS mountpoints, not legacy ones with fileSystems entries, so systemd
  #     generates no .mount units and those After= lines referenced nothing.
  #   - `zfs-mount.service` exits 0 even when the pool import FAILED, so docker
  #     started anyway and the empty-mountpoint race survived.
  #   - `Requires=zfs-mount.service` propagated any zfs-mount restart to all 39
  #     containers.
  #
  # Instead: verify the pools are genuinely imported AND mounted, as docker's own
  # ExecStartPre.
  #
  # This was briefly a separate `zfs-ready` oneshot that docker declared
  # `Requires=`. That worked at boot but introduced a new problem: `Requires=`
  # carries reverse STOP propagation, and NixOS defaults `stopIfChanged=true`, so
  # any `nixos-rebuild switch` that touched the helper stop-started it and took
  # all 39 containers down with it. Docker has no live-restore here, so that is a
  # real outage triggered by an unrelated config change.
  #
  # ExecStartPre keeps the identical boot-time guarantee — dockerd does not start
  # unless the check passes, on every start including socket activation — with no
  # unit to propagate from.
  systemd.services.docker.serviceConfig.ExecStartPre = [
    (pkgs.writeShellScript "docker-require-zfs" ''
      export PATH=${
        lib.makeBinPath [
          pkgs.zfs
          pkgs.util-linux
        ]
      }:$PATH
      for p in storage storage2; do
        if ! zpool list -H -o name "$p" >/dev/null 2>&1; then
          echo "pool $p is NOT imported — refusing to start docker" >&2
          exit 1
        fi
      done
      for m in /storage /storage2; do
        if ! mountpoint -q "$m"; then
          echo "$m is NOT a mountpoint — refusing to start docker" >&2
          exit 1
        fi
      done
      echo "both pools imported and mounted"
    '')
  ];

  # Ordering only — no Requires=, deliberately (see above). zfs-mount.service
  # exits 0 even when an import failed, so it is not a gate; ExecStartPre is.
  systemd.services.docker.after = [
    "zfs-import.target"
    "zfs-mount.service"
  ];

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

  # ---------------------------------------------------------------------------
  #  /etc/timezone — NixOS does not ship it, but containers expect it
  # ---------------------------------------------------------------------------
  # NixOS conveys the timezone via /etc/localtime and TZDIR; the Debian-style
  # /etc/timezone file simply does not exist. Several upstream container images
  # bind-mount it anyway (`- /etc/timezone:/etc/timezone:ro`).
  #
  # When a bind SOURCE does not exist, docker does not error — it silently
  # creates it, and creates it as a DIRECTORY. Found 2026-08-06: komga had
  # /etc/timezone bound, so docker made an empty directory at that path on a
  # host whose /etc is otherwise declaratively managed, and komga ran in UTC
  # with an empty TZ — seven hours off, with nothing reporting a problem.
  #
  # Same silent-auto-create failure as the `~` expansion trap that left four
  # media services on blank configs the same day (see RESTORE-RUNBOOK.md).
  # Providing the file removes the whole class for any container that wants it.
  environment.etc."timezone".text = "${config.time.timeZone}\n";

  # ---------------------------------------------------------------------------
  #  Legacy iptables tables — required by binhex/arch-delugevpn
  # ---------------------------------------------------------------------------
  # `deluge-vpn` (binhex/arch-delugevpn) installs its own kill-switch with the
  # LEGACY iptables binary, and refuses to start without it:
  #
  #   iptables v1.8.11 (legacy): can't initialize iptables table `filter':
  #     Table does not exist (do you need to insmod?)
  #   [crit] iptables default policies not available, exiting script...
  #
  # A container cannot fix this itself — even `privileged: true` does not help,
  # because the x_tables tables are created by HOST kernel modules and this host
  # boots with only `nf_tables` loaded. The container is privileged already and
  # still failed, which is what rules out a capability problem.
  #
  # Loading these is compatible with the rest of the system: the nft and legacy
  # backends can coexist, and nothing here switches the host's own firewall away
  # from nftables. This only makes the legacy tables *exist* so the container's
  # kill-switch can install rules in its own network namespace.
  #
  # ⛔ DO NOT re-add `boot.kernelModules = [ "ip_tables" ... ]` — TRIED AND IT DOES
  # NOT WORK (2026-08-06). Those modules are NOT SHIPPED by this kernel, so
  # systemd-modules-load merely logs six "Failed to find module" lines every boot
  # and changes nothing. Verified rather than assumed:
  #
  #   find -L /run/booted-system/kernel-modules/lib/modules/6.18.42 -name '*.ko*'
  #     -> 7303 modules, of which the only x_tables-family member is
  #        kernel/net/netfilter/x_tables.ko.xz
  #   modprobe ip_tables -> "Module ip_tables not found"
  #
  # `x_tables` IS loaded, but only as backing for `nft_compat` (19 users). The
  # legacy IPv4 table modules (ip_tables/iptable_filter/iptable_nat) are absent
  # even though /proc/config.gz advertises CONFIG_IP_NF_IPTABLES=m.
  #
  # The fix belongs in the CONTAINER, not here: the binhex image already ships
  # `xtables-nft-multi` and `iptables-nft`; it just symlinks /usr/sbin/iptables to
  # `xtables-legacy-multi` by default. Repoint those symlinks and its kill-switch
  # installs into nftables like the rest of this host. See the deluge-vpn service
  # in ~/git/docker/media/docker-compose.yaml.

  # ---------------------------------------------------------------------------
  #  Host-side name resolution — replaces the `host-hostnames` container
  # ---------------------------------------------------------------------------
  # The old openSUSE `infra` stack ran `dvdarias/docker-hoster`, which watched the
  # docker socket and rewrote /etc/hosts with live container names. It was REMOVED
  # from ~/git/docker/infra on 2026-08-06 because it cannot work here, for two
  # independent reasons:
  #
  #   1. Its bundled docker-py speaks Docker API 1.35. Docker 29.x refuses:
  #        "client version 1.35 is too old. Minimum supported API version is 1.40"
  #      so it crash-looped immediately on every start.
  #   2. It bind-mounts /etc/hosts read-write. On NixOS /etc/hosts is a symlink to
  #      /etc/static/hosts in the read-only nix store — a container whose whole job
  #      is rewriting that file is fundamentally at odds with declarative /etc.
  #
  # This is NOT a literal reimplementation, and deliberately so: docker-hoster
  # mapped *container names* to their current container IPs, which are dynamic and
  # cannot be expressed statically. What actually matters on this host is reaching
  # the traefik-published service names, so those are pinned to the LAN address
  # instead. That also avoids depending on hairpin NAT for host- and LAN-side
  # access, which the public records would otherwise require.
  #
  # Keep this list in step with the traefik `Host(...)` rules in ~/git/docker/*.
  # Regenerate with:
  #   grep -rhoE 'Host\(`[^`]+`\)' ~/git/docker/*/docker-compose.y*ml \
  #     | sed -E 's/Host\(`(.*)`\)/\1/' | sort -u
  # Hostnames come from ./traefik-hostnames.nix, shared with the coredns-custom
  # ConfigMap generated in pelargir/manifests.nix so the HOST and k3s PODS can never
  # disagree about where these services live.
  networking.hosts = {
    "10.0.1.6" = import ./traefik-hostnames.nix;
  };
}
