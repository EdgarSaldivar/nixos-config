{
  description = "Edgar's NixOS and nix-darwin configurations";

  # nixos-raspberrypi 67616c2 publishes its matched vendor kernels in its own
  # Cachix. Retain nix-community for the rest of this flake; INSTALL-RUNBOOK
  # must mirror both trusts before a fresh installer evaluates the flake.
  nixConfig = {
    extra-substituters = [
      "https://nix-community.cachix.org"
      "https://nixos-raspberrypi.cachix.org"
    ];
    extra-trusted-public-keys = [
      "nix-community.cachix.org-1:mB9FSh9qf2dCimDSUo8Zy7bkq5CX+/rkCWyvRCYg3Fs="
      "nixos-raspberrypi.cachix.org-1:4iMO9LXa8BqhU+Rpg6LQKiGa2lsNh/j2oiYLNOQ5sPI="
    ];
  };

  inputs = {
    # Current release. New and ported hosts track this.
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

    # Darwin needs the nixpkgs-*-darwin branch, NOT nixos-*. Mixing the two
    # is what previously broke the Linux hosts: every host was pointed at
    # nixpkgs-24.11-darwin to make dol-amroth work.
    #
    # Kept ALIGNED with the Linux release (26.05). Darwin sat on 24.11 while
    # Linux moved to 26.05, which meant the two halves of this flake were 18
    # months apart and dol-amroth was quietly running an unmaintained release.
    nixpkgs-darwin.url = "github:NixOS/nixpkgs/nixpkgs-26.05-darwin";

    nix-darwin = {
      url = "github:nix-darwin/nix-darwin/nix-darwin-26.05";
      inputs.nixpkgs.follows = "nixpkgs-darwin";
    };

    disko = {
      url = "github:nix-community/disko";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    home-manager = {
      url = "github:nix-community/home-manager/release-26.05";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    sops = {
      url = "github:Mic92/sops-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    nixos-raspberrypi = {
      # Pin the 2026-08-01 default-branch tip: 67616c2 makes the matched Pi
      # vendor kernel/firmware bundle default to 6.18.39. A full rev keeps the
      # direct-NVMe boot contract reviewable instead of drifting with upstream.
      url = "github:nvmd/nixos-raspberrypi/67616c24ed74573750f4864abfc358296a077466";
    };
  };

  outputs =
    inputs@{ nixpkgs, nix-darwin, ... }:
    let
      inherit (import ./lib/mkHost.nix { inherit inputs; }) mkNixos;
      lib = nixpkgs.lib;
      # Everything here must be EVALUATED on a Mac, so checks are eval-only and
      # never realize a Linux derivation.
      devSystem = "aarch64-darwin";
      devPkgs = nixpkgs.legacyPackages.${devSystem};
    in
    rec {
      nixosConfigurations = {
        nardol = mkNixos {
          system = "x86_64-linux";
          modules = [ ./hosts/nixos/nardol ];
        };

        # Ported to 26.05 on 2026-07-30 as part of the raz-server -> NixOS
        # rebuild. Real hardware (ASRock Rack X570D4U / 5950X), not the old VM
        # skeleton. See hosts/nixos/minas-tirith/disko.nix before any disk work:
        # nine of its ten drives are live ZFS pool members.
        minas-tirith = mkNixos {
          system = "x86_64-linux";
          modules = [ ./hosts/nixos/minas-tirith ];
        };

        # GMKtec NucBox G10 Frigate host. The internal AirDisk NVMe is the sole
        # install target; its external Sabrent recording SSD is mount-only.
        osgiliath = mkNixos {
          system = "x86_64-linux";
          modules = [ ./hosts/nixos/osgiliath ];
        };

        # Raspberry Pi 5. Built through nixos-raspberrypi's OWN `nixosSystem`
        # wrapper (see lib/mkHost.nix `builder`): its board modules only
        # evaluate with the overlays that wrapper injects, and it sets
        # `hostPlatform` itself, so no `system` is passed here.
        #
        # Consequence, stated plainly: this host's package set comes from the
        # framework's nixpkgs pin, NOT this flake's 26.05. That is the only
        # combination upstream tests and binary-caches, and matches what the
        # NixOS-on-Pi-5 wiki independently recommends. pelargir is a
        # single-purpose appliance, so the divergence is contained here.
        pelargir = mkNixos {
          builder = inputs.nixos-raspberrypi.lib.nixosSystem;
          modules = [ ./hosts/nixos/pelargir ];
        };

        # Remaining unported hosts (builder-vm, minas-tirith-vm, osgiliath-vm,
        # pelargir-vm) are deliberately not wired up. Their sources are
        # still in hosts/nixos/, and the last state where they were declared
        # is on the `legacy/24.11` branch. Revive them one at a time by
        # porting to 26.05, rather than dragging eight broken hosts forward.
      };

      darwinConfigurations = {
        dol-amroth = nix-darwin.lib.darwinSystem {
          system = "aarch64-darwin";
          modules = [
            inputs.home-manager.darwinModules.home-manager
            ./hosts/darwin/dol-amroth
          ];
        };
      };

      formatter.${devSystem} = devPkgs.nixfmt-rfc-style;

      # -----------------------------------------------------------------------
      # checks — invariants that must hold on EVERY change. `nix flake check`.
      # -----------------------------------------------------------------------
      # These exist because the most dangerous things in this repo were verified
      # by hand, repeatedly, and a hand check is only as good as the last person
      # who remembered to run it. Both checks below encode a mistake that was
      # actually made here.
      checks.${devSystem} = {
        # A flake output named `foo` whose networking.hostName is `bar` produces
        # a machine you deploy by one name and that calls itself another. Caught
        # exactly this: dol-amroth was configured as "dol-amorth" for months.
        hostnames =
          let
            mismatched = lib.filterAttrs (n: c: c.config.networking.hostName != n) (
              nixosConfigurations // darwinConfigurations
            );
            names = lib.mapAttrsToList (n: c: "${n} -> ${c.config.networking.hostName}") mismatched;
          in
          if mismatched == { } then
            devPkgs.runCommand "hostnames-ok" { } "touch $out"
          else
            throw "flake output name != networking.hostName for: ${lib.concatStringsSep ", " names}";

        # Nardol currently contains three NVMes. The serial-qualified Samsung
        # 970 EVO Plus and WD SN850X are intentionally disposable during this
        # migration; the Crucial P3 Plus must remain invisible to disko. Also
        # pin both intended LUKS2 -> ext4 shapes mechanically.
        nardol-disko-targets =
          let
            disks = nixosConfigurations.nardol.config.disko.devices.disk;
            devices = lib.mapAttrs (_: d: d.device) disks;
            expected = {
              fast = "/dev/disk/by-id/nvme-WD_BLACK_SN850X_4000GB_24160W802539";
              root = "/dev/disk/by-id/nvme-Samsung_SSD_970_EVO_Plus_2TB_S6S2NS0T629836M";
            };
            zpools = nixosConfigurations.nardol.config.disko.devices.zpool or { };
            rootLuks = disks.root.content.partitions.root.content;
            fastLuks = disks.fast.content.partitions.fast.content;
            rootFs = rootLuks.content;
            fastFs = fastLuks.content;
            isLuks2 =
              luks:
              luks.type == "luks"
              &&
                luks.extraFormatArgs == [
                  "--type"
                  "luks2"
                ];
          in
          if
            builtins.attrNames disks != [
              "fast"
              "root"
            ]
          then
            throw "nardol disko must declare exactly the disks named fast and root"
          else if devices != expected then
            throw "nardol disko target set changed: ${builtins.toJSON devices}"
          else if zpools != { } then
            throw "nardol declares disko.devices.zpool (${toString (builtins.attrNames zpools)}) — no zpool may be managed"
          else if !isLuks2 rootLuks || !isLuks2 fastLuks then
            throw "nardol managed partitions must be explicitly formatted as LUKS2"
          else if
            rootLuks.name != "nardol-root"
            || rootFs.type != "filesystem"
            || rootFs.format != "ext4"
            || rootFs.mountpoint != "/"
          then
            throw "nardol root must be the nardol-root LUKS mapper containing ext4"
          else if
            fastLuks.name != "nardol-fast"
            || fastFs.type != "filesystem"
            || fastFs.format != "ext4"
            || fastFs.mountpoint != "/srv"
          then
            throw "nardol fast must be the nardol-fast LUKS mapper containing ext4 at /srv"
          else
            devPkgs.runCommand "nardol-disko-targets-ok" { } "touch $out";

        # Disk encryption is useful on this headless host only while unattended
        # unlock and both manual recovery paths remain wired exactly as designed.
        nardol-unlock-contract =
          let
            cfg = nixosConfigurations.nardol.config;
            initrd = cfg.boot.initrd;
            lan = initrd.systemd.network.networks."10-nardol-lan";
            ssh = initrd.network.ssh;
          in
          if !initrd.clevisLuksAskpass.enable || !initrd.clevisLuksAskpass.useTang then
            throw "nardol unattended LUKS unlock must use Clevis Tang askpass"
          else if
            !initrd.systemd.enable
            || !initrd.systemd.network.enable
            || initrd.network.enable
            || initrd.systemd.network.networks."99-ethernet-default-dhcp".enable
            || initrd.systemd.network.networks."99-wireless-client-dhcp".enable
          then
            throw "nardol unlock must use only systemd networking in the initrd"
          else if
            lan.matchConfig.MACAddress != "9c:6b:00:36:e0:e8"
            || lan.address != [ "10.0.0.118/24" ]
            || lan.routes != [ ]
          then
            throw "nardol initrd LAN identity/address changed or gained a route"
          else if !lib.elem "igb" initrd.availableKernelModules then
            throw "nardol initrd is missing the Intel I211 igb driver"
          else if
            !ssh.enable
            || ssh.port != 2222
            || ssh.hostKeys != [ "/etc/secrets/initrd/ssh_host_ed25519_key" ]
            || !lib.all (lib.hasInfix ''command="/bin/systemd-tty-ask-password-agent"'') ssh.authorizedKeys
          then
            throw "nardol restricted initrd SSH recovery contract changed"
          else
            devPkgs.runCommand "nardol-unlock-contract-ok" { } "touch $out";

        # Headless gaming deliberately avoids a display manager/DE while
        # keeping the exact GPU, input, persistent-state, and container
        # contracts Wolf needs. Catch a future "cleanup" that silently puts
        # Docker back on root or turns the pinned container privileged again.
        nardol-gaming-contract =
          let
            cfg = nixosConfigurations.nardol.config;
            nardolPkgs = nixosConfigurations.nardol.pkgs;
            nvidia = cfg.hardware.nvidia;
            wolf = cfg.virtualisation.oci-containers.containers.wolf;
            expectedWolfImage = "ghcr.io/games-on-whales/wolf@sha256:ff82c125c9b79b2e9443de2b0eaec40c904edb03291680d408cccd57c1d59c76";
            expectedPulseImage = "ghcr.io/games-on-whales/pulseaudio@sha256:5f05a7102bdb6c464a96cb33770eb10c7fb6ca0c007961e3edd5915907643bed";
            expectedGamingDirectories = [
              "d /srv/games/steamapps 0750 1000 1000 - -"
              "d /srv/games/steamapps/.nardol-mod-staging 0750 1000 1000 - -"
              "d /srv/mods 0750 1000 1000 - -"
              "d /srv/mods/backups 0750 1000 1000 - -"
              "d /srv/mods/downloads 0750 1000 1000 - -"
              "d /srv/mods/logs 0750 1000 1000 - -"
              "d /srv/mods/manifests 0750 1000 1000 - -"
              "d /srv/mods/tools 0750 1000 1000 - -"
            ];
            wolfPreStart = cfg.systemd.services.docker-wolf.serviceConfig.ExecStartPre or [ ];
            wakeLink = cfg.systemd.network.links."10-nardol-i211-wake";
          in
          if cfg.services.xserver.enable then
            throw "nardol must remain headless; the NVIDIA selector must not enable X11"
          else if cfg.programs.steam.enable || !cfg.hardware.steam-hardware.enable then
            throw "nardol must keep only Steam hardware rules; the client belongs inside Wolf"
          else if
            !nvidia.open
            || !nvidia.modesetting.enable
            || !nvidia.nvidiaPersistenced
            || nvidia.nvidiaSettings
            || nvidia.package.outPath != cfg.boot.kernelPackages.nvidiaPackages.production.outPath
          then
            throw "nardol NVIDIA must use the headless open-module production-driver contract"
          else if
            !cfg.hardware.nvidia-container-toolkit.enable
            || cfg.virtualisation.docker.enableNvidia
            || cfg.virtualisation.docker.daemon.settings.data-root != "/srv/docker"
            || !(cfg.virtualisation.docker.daemon.settings.runtimes ? nvidia)
            || !lib.elem "/srv/docker" cfg.systemd.services.docker.unitConfig.RequiresMountsFor
          then
            throw "nardol Docker GPU/runtime or SN850X data-root contract changed"
          else if
            wolf.image != expectedWolfImage
            || wolf.pull != "missing"
            || wolf.privileged
            || wolf.networks != [ "host" ]
            || wolf.environment.HOST_APPS_STATE_FOLDER != "/var/lib/wolf"
            || wolf.environment.WOLF_PULSE_IMAGE != expectedPulseImage
            || !lib.any (lib.hasInfix "nardol-wolf-config-policy") wolfPreStart
            || !lib.elem "/srv/wolf/config:/etc/wolf:rw" wolf.volumes
            || !lib.elem "/srv/wolf/data:/var/lib/wolf:rw" wolf.volumes
            || !lib.elem "/dev/uinput:/dev/uinput" wolf.devices
            || !lib.elem "/dev/uhid:/dev/uhid" wolf.devices
            || !lib.all (rule: lib.elem rule cfg.systemd.tmpfiles.rules) expectedGamingDirectories
          then
            throw "nardol Wolf image policy, privilege, state, network, or input contract changed"
          else if
            !cfg.hardware.uinput.enable
            || !lib.elem "uhid" cfg.boot.kernelModules
            || cfg.users.users.edgar.uid != 1000
            || cfg.users.groups.edgar.gid != 1000
            || wakeLink.matchConfig.MACAddress != "9c:6b:00:36:e0:e8"
            || wakeLink.linkConfig.WakeOnLan != "magic"
            || !lib.elem nardolPkgs.ethtool cfg.environment.systemPackages
          then
            throw "nardol Wolf input, persistent UID/GID, or headless wake contract changed"
          else
            devPkgs.runCommand "nardol-gaming-contract-ok" { } "touch $out";

        # Tang is intentionally a narrow LAN-only boot dependency, and its key
        # directory must enter Pelargir's existing off-host restic recovery set.
        pelargir-tang-contract =
          let
            cfg = nixosConfigurations.pelargir.config;
            tang = cfg.services.tang;
            tangService = cfg.systemd.services."tangd@".serviceConfig;
            restic = cfg.systemd.services.restic-backups-minas;
          in
          if
            !tang.enable
            || tang.listenStream != [ "0.0.0.0:7654" ]
            ||
              tang.ipAddressAllow != [
                "127.0.0.1/32"
                "10.0.0.118/32"
              ]
          then
            throw "pelargir Tang listener or systemd source ACL changed"
          else if tangService.RuntimeDirectoryPreserve != true then
            throw "pelargir Tang must preserve its shared runtime directory across Accept=yes instances"
          else if
            !lib.hasInfix ''iifname "eth0" ip saddr 10.0.0.118 tcp dport 7654 accept'' cfg.networking.firewall.extraInputRules
          then
            throw "pelargir Tang lost its source-scoped nftables rule"
          else if
            !lib.elem "pelargir-stage-tang-state.service" restic.requires
            || !lib.elem "pelargir-stage-tang-state.service" restic.after
          then
            throw "pelargir restic no longer requires the Tang key staging service"
          else
            devPkgs.runCommand "pelargir-tang-contract-ok" { } "touch $out";

        # THE important one. disko destroys exactly the disks it is handed, so
        # the only thing standing between a config edit and nine live ZFS pool
        # members (~98 TB, no backup) is that this list stays correct. Assert it
        # mechanically instead of re-reading it by eye before each install.
        minas-tirith-disko-targets =
          let
            disks = nixosConfigurations.minas-tirith.config.disko.devices.disk;
            devices = lib.mapAttrsToList (_: d: d.device) disks;
            expected = [ "/dev/disk/by-id/nvme-Samsung_SSD_980_1TB_S64ANS0RA05335R" ];
            zpools = nixosConfigurations.minas-tirith.config.disko.devices.zpool or { };
          in
          if devices != expected then
            throw "minas-tirith disko would touch ${toString devices} — expected exactly ${toString expected}"
          else if zpools != { } then
            throw "minas-tirith declares disko.devices.zpool (${toString (builtins.attrNames zpools)}) — disko must never manage the existing pools"
          else
            devPkgs.runCommand "minas-tirith-disko-targets-ok" { } "touch $out";

        # Pelargir has one disposable install target, but the short nvme name is
        # still unsafe: PCI discovery order can change across EEPROM/kernel
        # updates. Keep the serial-qualified device mechanically pinned.
        pelargir-disko-targets =
          let
            disks = nixosConfigurations.pelargir.config.disko.devices.disk;
            devices = lib.mapAttrsToList (_: d: d.device) disks;
            expected = [ "/dev/disk/by-id/nvme-KINGSTON_SNVS1000G_50026B7685D2B59A" ];
            zpools = nixosConfigurations.pelargir.config.disko.devices.zpool or { };
          in
          if devices != expected then
            throw "pelargir disko would touch ${toString devices} — expected exactly ${toString expected}"
          else if zpools != { } then
            throw "pelargir declares disko.devices.zpool (${toString (builtins.attrNames zpools)}) — this host has no disko-managed ZFS pools"
          else
            devPkgs.runCommand "pelargir-disko-targets-ok" { } "touch $out";

        # Osgiliath's external Sabrent SSD contains existing Frigate recordings
        # and must remain outside disko. Only the serial-pinned internal NVMe is
        # an installation target, and this host has no disko-managed ZFS pool.
        osgiliath-disko-targets =
          let
            disks = nixosConfigurations.osgiliath.config.disko.devices.disk;
            devices = lib.mapAttrsToList (_: d: d.device) disks;
            expected = [ "/dev/disk/by-id/nvme-AirDisk_1TB_SSD_QEK975R001121P1129" ];
            zpools = nixosConfigurations.osgiliath.config.disko.devices.zpool or { };
          in
          if devices != expected then
            throw "osgiliath disko would touch ${toString devices} — expected exactly ${toString expected}"
          else if zpools != { } then
            throw "osgiliath declares disko.devices.zpool (${toString (builtins.attrNames zpools)}) — this host has no disko-managed ZFS pools"
          else
            devPkgs.runCommand "osgiliath-disko-targets-ok" { } "touch $out";
      };
    };
}
