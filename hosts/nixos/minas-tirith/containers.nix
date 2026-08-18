# minas-tirith — GPU enablement and host-level container settings.
#
# ⛔ DOCKER WAS REMOVED FROM THIS HOST on 2026-08-17 (ROADMAP item 12). What this
# file used to carry -- `virtualisation.docker`, its daemon.settings block, and an
# ExecStartPre that refused to start dockerd unless both ZFS pools were imported
# and mounted -- is gone, because there is nothing left to run:
#
#   31 stopped containers  removed 2026-08-16
#   ~80 GB of images       reclaimed 2026-08-16
#   61 named volumes       reclaimed 2026-08-17, after verifying file-count parity
#                          against /storage2/backup/minas-tirith/volumes
#
# ⚠️ WHAT STAYED, AND WHY IT MUST: everything below is GPU, and k3s needs it.
# `hardware.nvidia-container-toolkit` is CDI-only on NixOS -- it emits the spec at
# /var/run/cdi/nvidia-container-toolkit.json that k3s-gpu.nix's containerd runtime
# shim reads. Removing it because the name contains "container" would take GPU
# transcode away from Plex and Jellyfin, which are now Pods.
#
# The boot-ordering guarantee the deleted ExecStartPre gave is not lost with it.
# It existed because docker would CREATE EMPTY DIRECTORIES over an unmounted pool
# and the compose stacks would then serve empty storage. The Pods that replaced
# them mount /storage and /storage2 as hostPath `type: Directory`, so the kubelet
# refuses to start them instead of papering over the missing mount.
#
# ⚠️ nardol still runs docker, for wolf. This removal is minas-only.
{
  config,
  lib,
  pkgs,
  ...
}:
{

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
  # media services on blank configs the same day (see docs/runbooks/minas-tirith/backup-restore.md).
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
