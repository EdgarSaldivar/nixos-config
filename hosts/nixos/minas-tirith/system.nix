# minas-tirith (raz-server) — system, identity and networking.
{ pkgs, ... }:
{
  # ---------------------------------------------------------------------------
  # Identity
  # ---------------------------------------------------------------------------
  # Renamed from raz-server -> minas-tirith as of the NixOS rebuild. Verified
  # safe: nothing in the five docker-compose stacks references the old name (only
  # /etc/hostname and a historical git author string did), and the
  # host-hostnames helper (dvdarias/docker-hoster) only writes *container* names
  # into /etc/hosts. The public DNS name minas.saldivar.io already matches.
  #
  # The ZFS pool labels still record hostname 'raz-server'; they will record this
  # name after the clean export/import handover described in ./zfs.nix.
  networking.hostName = "minas-tirith";
  networking.domain = "saldivar.io";

  # Required for ZFS. The value itself is arbitrary as long as it never changes
  # — after the clean export/import handover (see ./zfs.nix) the pools record
  # whatever this host presents. This is the value the old install used, kept
  # for continuity.
  networking.hostId = "0149f5f3";

  time.timeZone = "America/Los_Angeles";
  system.stateVersion = "26.05";
  nixpkgs.config.allowUnfree = true;

  # ---------------------------------------------------------------------------
  # Networking — systemd-networkd, matched by MAC not by interface name.
  # ---------------------------------------------------------------------------
  # This board renumbers PCI devices (see pci=realloc=off in
  # hardware-configuration.nix) and a rename has broken networking on Edgar's
  # hardware before. Matching on MAC means the config survives enp38s0 becoming
  # something else.
  #
  # Static rather than DHCP: the old install relied on a DHCP reservation on a
  # LAN whose owner reconfigures things. A server should not depend on that.
  networking.useDHCP = false;
  networking.useNetworkd = true;
  systemd.network.enable = true;

  systemd.network.networks."10-lan" = {
    matchConfig.MACAddress = "a8:a1:59:c0:4e:73"; # was enp38s0
    address = [ "10.0.1.6/20" ];
    routes = [
      { Gateway = "10.0.0.1"; }
      # WireGuard client subnet. Without this route, VPN return traffic is
      # blackholed — this is also why Docker's address pool is pinned away from
      # 192.168.x (see ./docker.nix).
      {
        Destination = "192.168.4.0/24";
        Gateway = "10.0.0.1";
      }
    ];
    networkConfig = {
      DNS = [ "10.0.0.1" ];
      IPv6AcceptRA = true;
    };
    linkConfig.RequiredForOnline = "routable";
  };

  # Second onboard NIC (a8:a1:59:c0:4e:72) is unused and unplugged. Declared so
  # boot does not wait on it.
  systemd.network.networks."20-lan-unused" = {
    matchConfig.MACAddress = "a8:a1:59:c0:4e:72";
    linkConfig.ActivationPolicy = "down";
    linkConfig.RequiredForOnline = "no";
  };

  # ---------------------------------------------------------------------------
  # Access
  # ---------------------------------------------------------------------------
  services.openssh = {
    enable = true;
    settings = {
      # The old box was taking SSH brute-force on an internet-facing forward
      # with password auth enabled. Not repeating that.
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      PermitRootLogin = "no";
    };
  };

  # NOTE: restore the old SSH host keys from the backup after install if you
  # want existing known_hosts entries and automation to keep working:
  #   /storage2/backup-2026-07-30/etc/ssh/ssh_host_*
  # Otherwise every client sees a host key change on first connect.

  security.sudo = {
    enable = true;
    wheelNeedsPassword = false;
  };

  # Console login password is provided via sops — see ./secrets.nix.
  # (mutableUsers = false + key-only sshd would otherwise leave the SOL console
  # showing a login prompt nobody can satisfy.)


  # ---------------------------------------------------------------------------
  # Nix itself
  # ---------------------------------------------------------------------------
  # Without this the host cannot use flake commands on itself — which matters,
  # because `nixos-rebuild --flake` from the machine is the recovery path when
  # deploying from the Mac isn't possible.
  nix.settings.experimental-features = [
    "nix-command"
    "flakes"
  ];

  # A server accumulating generations on a 931 GB root that was already ~660 GB
  # full will eventually wedge on disk space. systemd-boot keeps 10 generations
  # (see ./boot.nix); this bounds the store.
  nix.gc = {
    automatic = true;
    dates = "weekly";
    options = "--delete-older-than 30d";
  };
  nix.settings.auto-optimise-store = true;

  # ---------------------------------------------------------------------------
  # Hardware watchdog
  # ---------------------------------------------------------------------------
  # This machine has hung hard before (2026-07-30 `OS Critical Stop`, and a
  # kernel that reached the GRUB prompt and sat there). It is remote, and BMC
  # power-cycling requires a human to notice first. The X570D4U exposes the
  # SP5100 TCO watchdog — with systemd petting it, a hung kernel reboots itself
  # instead of waiting to be discovered.
  boot.kernelModules = [ "sp5100_tco" ];
  systemd.watchdog = {
    runtimeTime = "30s";
    rebootTime = "10min";
  };

  # ---------------------------------------------------------------------------
  # Observability — the old box had none, which is why an outage was found by
  # hand and filesystem damage sat unnoticed between monthly scrubs.
  # ---------------------------------------------------------------------------
  # rasdaemon persists and decodes machine checks. The old host took a fatal
  # uncorrectable MCE (Bank 5 / Execution Unit, PCC set) on 2026-07-28 with
  # nothing recording it.
  hardware.rasdaemon.enable = true;

  # smartd for the NVMe and the nine HBA disks. /dev/sdc has had 24 pending +
  # 9 offline-uncorrectable sectors and is inside the raidz2.
  services.smartd = {
    enable = true;
    autodetect = true;
  };

  # Weekly trim rather than the continuous `discard` mount option: on a
  # DRAM-less QLC-adjacent drive, inline discard adds latency and write
  # amplification. Pairs with `nodiscard` in ./disko.nix.
  services.fstrim = {
    enable = true;
    interval = "weekly";
  };

  # ext4 does not checksum file DATA (only metadata), so silent data corruption
  # would not announce itself the way btrfs did. The compensating controls are
  # backups and database integrity checks — this is the accepted tradeoff of
  # keeping all service data on a single root device.
  #
  # Nightly snapshot of the mutable service data onto the redundant, checksummed
  # ZFS pool. Cheap insurance for the thing ext4 cannot tell us about.
  systemd.services.backup-root-data = {
    description = "Back up mutable service data from root to ZFS storage2";
    serviceConfig = {
      Type = "oneshot";
      Nice = 10;
      IOSchedulingClass = "idle";
    };
    script = ''
      set -euo pipefail

      # REFUSE to run unless storage2 is genuinely a mounted ZFS filesystem.
      # Without this check, a boot where the pool failed to import would send
      # ~429 GB straight onto the root NVMe (931 GB total, ~660 GB already used)
      # and fill it — turning a recoverable pool problem into a dead root.
      if ! ${pkgs.util-linux}/bin/findmnt -no FSTYPE /storage2 | grep -qx zfs; then
        echo "ABORT: /storage2 is not a mounted ZFS filesystem; refusing to write to root" >&2
        exit 1
      fi

      dest=/storage2/backup/minas-tirith
      mkdir -p "$dest"
      ${pkgs.rsync}/bin/rsync -aHAX --delete --inplace \
        --exclude='*/Cache/***' --exclude='*/transcode/***' \
        /etc /home /usr/local /opt /srv /var/lib/docker/volumes "$dest/"
    '';
  };
  systemd.timers.backup-root-data = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "daily";
      Persistent = true;
      RandomizedDelaySec = "30m";
    };
  };

  environment.systemPackages = with pkgs; [
    # storage / diagnostics
    zfs
    smartmontools
    nvme-cli
    cryptsetup
    lsof
    pciutils
    usbutils
    # BMC — ipmitool could not be installed on the old box because a devel-repo
    # glibc pin blocked it; freeipmi was the workaround. Both, here.
    ipmitool
    freeipmi
    # basics
    git
    wget
    curl
    tmux
    htop
    python3
    sops
    age
  ];
}
