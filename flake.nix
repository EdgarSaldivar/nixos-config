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
        wolf-reconciler =
          let
            python = devPkgs.python3.withPackages (pythonPackages: [
              pythonPackages.pytest
              pythonPackages.tomlkit
            ]);
          in
          devPkgs.runCommand "wolf-reconciler-tests" { nativeBuildInputs = [ python ]; } ''
            export HOME="$TMPDIR/home"
            mkdir -p "$HOME"
            python -m pytest -q ${./hosts/nixos/nardol}/tests
            touch "$out"
          '';

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

        # A digest alone identifies bytes, but not the reviewed source revision the
        # image claims to contain. Require both published images to carry independently
        # recorded full Git revisions that match the one approved for the release.
        pin-collector-release-contract =
          let
            contract = import ./hosts/nixos/minas-tirith/pin-collector-release-contract.nix {
              inherit lib;
            };
            shaA = lib.concatStrings (lib.replicate 40 "a");
            shaB = lib.concatStrings (lib.replicate 40 "b");
            valid = {
              staged = true;
              gitRevision = shaA;
              apiImageRevision = shaA;
              modelImageRevision = shaA;
            };
            inactive = {
              staged = false;
              gitRevision = null;
              apiImageRevision = null;
              modelImageRevision = null;
            };
            accepts = release: (builtins.tryEval (contract.assertValid release)).success;
          in
          if
            !accepts valid
            || !accepts inactive
            || accepts (inactive // { gitRevision = "malformed"; })
            || accepts (valid // { gitRevision = builtins.substring 0 39 shaA; })
            || accepts (valid // { apiImageRevision = "A${builtins.substring 1 39 shaA}"; })
            || accepts (valid // { modelImageRevision = builtins.substring 0 39 shaA; })
            || accepts (builtins.removeAttrs valid [ "apiImageRevision" ])
            || accepts (valid // { apiImageRevision = shaB; })
            || accepts (valid // { modelImageRevision = shaB; })
          then
            throw "PinCollector release revision contract accepted malformed or mismatched evidence"
          else
            devPkgs.runCommand "pin-collector-release-contract-ok" { } "touch $out";

        pin-collector-secret-contract =
          let
            manifestSource = builtins.readFile ./hosts/nixos/minas-tirith/manifests/pin-collector.yaml.in;
            secretsSource = builtins.readFile ./hosts/nixos/pelargir/secrets.nix;
            requiredManifestFragments = [
              "emptyDir: { medium: Memory, sizeLimit: 16Mi }"
              "rm -rf /tmp/mc"
              "trap cleanup EXIT"
              ''expected-image-revision: "@gitRevision@"''
            ];
            requiredSecretFragments = [
              "set -euo pipefail"
              "--from-file=postgres-password="
              "--from-file=hf-token="
              "--from-file=.dockerconfigjson="
              "| k3s kubectl apply -f -"
            ];
            forbiddenSecretFragments = [
              "placeholder.pin_collector"
              "pin-collector-secrets.yaml"
              "pin-collector-registry.yaml"
            ];
            missingManifest = lib.filter (
              fragment: !lib.hasInfix fragment manifestSource
            ) requiredManifestFragments;
            missingSecrets = lib.filter (
              fragment: !lib.hasInfix fragment secretsSource
            ) requiredSecretFragments;
            forbiddenSecrets = lib.filter (
              fragment: lib.hasInfix fragment secretsSource
            ) forbiddenSecretFragments;
          in
          if missingManifest != [ ] || missingSecrets != [ ] || forbiddenSecrets != [ ] then
            throw "PinCollector ephemeral-secret contract failed"
          else
            devPkgs.runCommand "pin-collector-secret-contract-ok" { } "touch $out";

        # local-path creates the volume root owned by uid 0, and fsGroup sets only the
        # group, so a Restricted-Pod-Security container cannot chmod the mount point.
        # initdb must own a subdirectory rather than the mount itself, or PostgreSQL
        # crash-loops on EPERM the first time the PVC is ever created.
        pin-collector-postgres-data-contract =
          let
            manifestSource = builtins.readFile ./hosts/nixos/minas-tirith/manifests/pin-collector.yaml.in;
            mountPath = "/var/lib/postgresql/data";
          in
          if
            !lib.hasInfix "{ name: PGDATA, value: ${mountPath}/pgdata }" manifestSource
            || !lib.hasInfix "{ name: data, mountPath: ${mountPath} }" manifestSource
          then
            throw "PinCollector PostgreSQL must initialise into a subdirectory of its volume mount"
          else
            devPkgs.runCommand "pin-collector-postgres-data-contract-ok" { } "touch $out";

        # A rotated secret only reaches the cluster if k3s-apply-secrets runs again.
        # It is a `oneshot` with `RemainAfterExit`, so activation skips it unless
        # something names it: changing a VALUE does not change the unit definition.
        # The failure is silent and looks like success -- the rebuild passes, sops
        # reports the new value, and the cluster keeps serving the old one. That
        # stranded a broken GHCR credential for two hours on 2026-08-15.
        # The trap belongs to the applier, not to PinCollector, so cover everything it
        # pushes: the pin_collector_* files and the three rendered Secret templates
        # (home, cluster-apps, authentik). Rotating a nextcloud or authentik value hits
        # exactly the same silent skip.
        secret-applier-contract =
          let
            pelargir = nixosConfigurations.pelargir.config;
            applied = "k3s-apply-secrets.service";
            script = pelargir.systemd.services.k3s-apply-secrets.script;
            # Derive what to protect from the applier ITSELF rather than from a name
            # pattern. Anything whose rendered path the script reads is pushed into the
            # cluster, so a future secret is covered no matter what it is called --
            # matching on a prefix would quietly exempt anything named differently.
            candidates =
              lib.mapAttrs' (n: v: lib.nameValuePair "secret ${n}" v) pelargir.sops.secrets
              // lib.mapAttrs' (n: v: lib.nameValuePair "template ${n}" v) pelargir.sops.templates;
            referenced = lib.filterAttrs (_: entry: lib.hasInfix entry.path script) candidates;
            missing = lib.attrNames (
              lib.filterAttrs (_: entry: !(lib.elem applied (entry.restartUnits or [ ]))) referenced
            );
          in
          # Guard the vacuous pass: if the script stops interpolating sops paths, this
          # check would otherwise succeed by protecting nothing at all.
          if referenced == { } then
            throw "${applied} references no sops paths; this contract would pass vacuously"
          else if missing != [ ] then
            throw "these must restart ${applied}: ${lib.concatStringsSep ", " missing}"
          else
            devPkgs.runCommand "secret-applier-contract-ok" { } "touch $out";

        # Every disk collector must retain a stable fleet identity, the private
        # endpoint, catch-up scheduling and its tailnet dependency. Keep the
        # central service host-native and prevent Nardol's collector role from
        # quietly turning it into a k3s node.
        fleet-disk-health =
          let
            expected = {
              minas-tirith = "minas-tirith";
              nardol = "nardol";
              osgiliath = "osgiliath";
              pelargir = "pelargir";
            };
            brokenCollectors = lib.filterAttrs (
              name: hostId:
              let
                cfg = nixosConfigurations.${name}.config;
                collector = cfg.services.scrutiny.collector;
                unit = cfg.systemd.services.scrutiny-collector;
                timer = cfg.systemd.timers.scrutiny-collector;
              in
              !cfg.fleet.diskHealth.enable
              || !cfg.services.tailscale.enable
              || !collector.enable
              || collector.package.version != "0.9.2"
              || collector.schedule != "hourly"
              || collector.settings.host.id != hostId
              || collector.settings.api.endpoint != "http://minas-tirith:9080"
              || collector.settings ? devices
              || !timer.timerConfig.Persistent
              || !lib.elem "network-online.target" unit.after
              || !lib.elem "tailscaled.service" unit.after
            ) expected;
            minas = nixosConfigurations.minas-tirith.config;
          in
          if brokenCollectors != { } then
            throw "fleet disk-health collector contract failed for: ${lib.concatStringsSep ", " (builtins.attrNames brokenCollectors)}"
          else if
            !minas.services.scrutiny.enable
            || minas.services.scrutiny.package.version != "0.9.2"
            || !minas.services.scrutiny.influxdb.enable
            || minas.services.scrutiny.settings.web.listen.host != "0.0.0.0"
            || minas.services.scrutiny.settings.web.listen.port != 9080
            || minas.services.scrutiny.settings.web.influxdb.host != "127.0.0.1"
            || minas.services.scrutiny.settings.user.metrics.repeat_notifications
            || minas.services.scrutiny.collector.settings.commands.metrics_scan_args != "--scan-open --json"
            || minas.services.influxdb2.settings."http-bind-address" != "127.0.0.1:8086"
            || minas.services.scrutiny.openFirewall
            || !lib.elem 9080 minas.networking.firewall.interfaces.tailscale0.allowedTCPPorts
            || lib.elem 9080 minas.networking.firewall.allowedTCPPorts
          then
            throw "minas-tirith Scrutiny must remain host-native, pinned, and tailnet-only"
          else if nixosConfigurations.nardol.config.services.k3s.enable then
            throw "Nardol's disk collector must not enable k3s"
          else
            devPkgs.runCommand "fleet-disk-health-ok" { } "touch $out";

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
            foreignPvFilter = ''devices/global_filter = [ "r|.*|" ]'';
            stage2LvmConfig = cfg.environment.etc."lvm/lvm.conf".text;
            initrdLvmConfig = initrd.systemd.contents."/etc/lvm/lvm.conf".text;
            usbKeyDevice = "/dev/disk/by-id/usb-General_USB_Flash_Disk_0305500000000280-0:0";
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
            # Nardol travels: the initrd takes a DHCP address so it works on a
            # foreign LAN, but must never accept routing or resolution from one.
            # All four of these are required -- UseGateway covers only the Router
            # option, UseRoutes covers option-121 classless routes, IPv6AcceptRA
            # covers router advertisements, and lan.routes covers declarative
            # ones. Dropping any single check leaves a way in.
            || lan.address != [ ]
            || lan.routes != [ ]
            || lan.networkConfig.DHCP != "ipv4"
            || lan.networkConfig.IPv6AcceptRA != false
            || lan.networkConfig.LinkLocalAddressing != "no"
            || lan.dhcpV4Config.UseGateway != false
            || lan.dhcpV4Config.UseRoutes != false
            || lan.dhcpV4Config.UseDNS != false
            || lan.dhcpV4Config.ClientIdentifier != "mac"
            # Off-site discovery is via the foreign router's lease table; this is
            # the label it appears under, so a silent removal breaks the runbook.
            || lan.dhcpV4Config.Hostname != "nardol-initrd"
          then
            throw "nardol initrd must take a DHCP address only, with no route, DNS, or RA"
          else if !lib.elem "igb" initrd.availableKernelModules then
            throw "nardol initrd is missing the Intel I211 igb driver"
          else if
            !lib.all (m: lib.elem m initrd.availableKernelModules) [
              "usb_storage"
              "xhci_pci"
              "sd_mod"
            ]
          then
            throw "nardol initrd is missing the USB modules needed to read the unlock key"
          else if
            let
              luksNames = [
                "nardol-root"
                "nardol-fast"
              ];
              dev = name: cfg.boot.initrd.luks.devices.${name};
            in
            !lib.all (
              name:
              (dev name).keyFile == usbKeyDevice
              && (dev name).keyFileSize == 4096
              && (dev name).keyFileOffset == 4194304
              && (dev name).keyFileTimeout != null
              && (dev name).keyFileTimeout > 0
              && !(dev name).fallbackToPassword
            ) luksNames
          then
            # keyFileTimeout is what makes a MISSING stick survivable: without it
            # systemd hard-depends on the by-id device and the boot hangs with no
            # console or SSH recovery. fallbackToPassword must stay false --
            # systemd stage 1 implies it and nixpkgs asserts it.
            throw "nardol USB unlock key contract changed on one or both volumes"
          else if
            !cfg.services.lvm.enable
            || !lib.hasInfix foreignPvFilter stage2LvmConfig
            || !lib.hasInfix foreignPvFilter initrdLvmConfig
          then
            throw "nardol must keep LVM udev support while rejecting all foreign PV scanning"
          else if
            !ssh.enable
            || ssh.port != 2222
            || ssh.hostKeys != [ "/etc/secrets/initrd/ssh_host_ed25519_key" ]
            || !lib.all (lib.hasInfix ''command="/bin/systemd-tty-ask-password-agent"'') ssh.authorizedKeys
            # Each forced option asserted individually: a single substring match
            # tolerates a key that quietly drops one of the others.
            || !lib.all (
              key:
              lib.all (opt: lib.hasInfix opt key) [
                "no-agent-forwarding"
                "no-port-forwarding"
                "no-X11-forwarding"
                "no-user-rc"
                ''from="''
              ]
            ) ssh.authorizedKeys
            # Key-only. The source restriction is now RFC1918-wide so a laptop at
            # a LAN party can reach the prompt, which makes these the real control.
            || !lib.all (d: lib.hasInfix d ssh.extraConfig) [
              "PasswordAuthentication no"
              "KbdInteractiveAuthentication no"
              "AuthenticationMethods publickey"
            ]
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
            expectedEglVendorFiles = "/run/opengl-driver/share/glvnd/egl_vendor.d/10_nvidia.json:/usr/share/glvnd/egl_vendor.d/50_mesa.json";
            expectedNvrtcContainerPath = "/opt/nardol-nvrtc";
            expectedNvrtcMount = "${nardolPkgs.cudaPackages.cuda_nvrtc.lib}/lib:${expectedNvrtcContainerPath}:ro";
            wolfPaths = import ./hosts/nixos/nardol/wolf-paths.nix;
            imagePins = {
              "ghcr.io/games-on-whales/es-de:edge" = "ghcr.io/games-on-whales/es-de@sha256:f5d1037e9dd6ff7406e190e00457152d0a9dcb4adbc32fe2132585cb5bbe7829";
              "ghcr.io/games-on-whales/firefox:edge" = "ghcr.io/games-on-whales/firefox@sha256:1ea7331934d31d346079fb67462b371586d65b5ebb792acee8c0e64e87c185b1";
              "ghcr.io/games-on-whales/kodi:edge" = "ghcr.io/games-on-whales/kodi@sha256:e3db2ca9492b85f98c253436c22d47c841009d278b7e8dc7f3f349aca2ebfe8a";
              "ghcr.io/games-on-whales/lutris:edge" = "ghcr.io/games-on-whales/lutris@sha256:207005d9e1a839814c7c2b91fa25190d40c388c7dc004eec593556bd807f99f2";
              "ghcr.io/games-on-whales/pegasus:edge" = "ghcr.io/games-on-whales/pegasus@sha256:29e7ab082f1c73a92ff25dff66983a83790d109ea49e0826cb0279b7fe5eacd8";
              "ghcr.io/games-on-whales/prismlauncher:edge" = "ghcr.io/games-on-whales/prismlauncher@sha256:e2c610f666b019a2e31482641cab6c3330a24add41fd88f939a15f327bf9dda0";
              "ghcr.io/games-on-whales/retroarch:edge" = "ghcr.io/games-on-whales/retroarch@sha256:bbcf4523e589fc7177b522ce56ba9507c6530caaf1999e37b37062a189f18cf2";
              "ghcr.io/games-on-whales/wolf-ui:main" = "ghcr.io/games-on-whales/wolf-ui@sha256:cd6de1158b29068e4a4d4ce6312976067517239be97200a391be758a6ddfcf9b";
              "ghcr.io/games-on-whales/xfce:edge" = "ghcr.io/games-on-whales/xfce@sha256:2ce1db7432bcb60caf5b3da23ea0ad5a24f300f3e7f346045fd6ba74a477ebcd";
              "ghcr.io/edgarsaldivar/nardol-steam-tools:git-214fce8091fc0524d64996a3b225ee3a98251c36" = "ghcr.io/edgarsaldivar/nardol-steam-tools@sha256:629951ab9461def4aa78424d45a5748c7a114b421a46c68a86609126cb1238d8";
            };
            imageTags = builtins.attrNames imagePins;
            wolfConfigText = builtins.replaceStrings imageTags (map (tag: imagePins.${tag}) imageTags) (
              builtins.readFile ./hosts/nixos/nardol/wolf-config.template.toml
            );
            wolfConfig = builtins.fromTOML wolfConfigText;
            validateWolfConfig = import ./hosts/nixos/nardol/wolf-validator.nix {
              inherit lib;
              paths = wolfPaths;
            };
            mapProfile = profileId: operation: configValue: configValue // {
              profiles = map (profile: if profile.id == profileId then operation profile else profile) configValue.profiles;
            };
            mapApp = profileId: title: operation: mapProfile profileId (profile: profile // {
              apps = map (app: if app.title == title then operation app else app) profile.apps;
            });
            addMount = mount: app: app // { runner = app.runner // { mounts = app.runner.mounts ++ [ mount ]; }; };
            malformedFixtures = [
              (wolfConfig // { profiles = wolfConfig.profiles ++ [ (builtins.head wolfConfig.profiles) ]; })
              (mapProfile "guest" (profile: profile // { apps = profile.apps ++ [ (builtins.head profile.apps) ]; }) wolfConfig)
              (mapApp "guest" "Steam" (app: app // { runner = app.runner // { mounts = map (mount: builtins.replaceStrings [ wolfPaths.guest.steamapps ] [ wolfPaths.user.steamapps ] mount) app.runner.mounts; }; }) wolfConfig)
              (mapApp "guest" "Steam" (addMount wolfPaths.user.mounts.mods) wolfConfig)
              (mapApp "guest" "Steam" (addMount "guest-data:/guest:rw") wolfConfig)
              (mapApp "guest" "Steam" (addMount "lutris:/var/lutris/:rw") wolfConfig)
              (mapApp "moonlight-profile-id" "Test ball" (app: app // { runner = app.runner // { run_cmd = "sh -c arbitrary-root-command"; }; }) wolfConfig)
              (mapApp "guest" "Steam" (app: app // { runner = app.runner // { type = "process"; }; }) wolfConfig)
              (mapApp "guest" "Steam" (app: app // { start_virtual_compositor = false; }) wolfConfig)
            ];
            expectedGamingDirectories = [
              "d ${wolfPaths.user.steamapps} 0750 1000 1000 - -"
              "d ${wolfPaths.user.nonsteam} 0750 1000 1000 - -"
              "d ${wolfPaths.user.modStaging} 0750 1000 1000 - -"
              "d ${wolfPaths.user.mods} 0750 1000 1000 - -"
              "d ${wolfPaths.user.backups} 0750 1000 1000 - -"
              "d ${wolfPaths.user.downloads} 0750 1000 1000 - -"
              "d ${wolfPaths.user.logs} 0750 1000 1000 - -"
              "d ${wolfPaths.user.manifests} 0750 1000 1000 - -"
              "d ${wolfPaths.user.tools} 0750 1000 1000 - -"
              "d ${wolfPaths.guest.steamapps} 0750 1000 1000 - -"
              "d ${wolfPaths.guest.nonsteam} 0750 1000 1000 - -"
              "d ${wolfPaths.guest.modStaging} 0750 1000 1000 - -"
              "d ${wolfPaths.guest.mods} 0750 1000 1000 - -"
              "d ${wolfPaths.guest.backups} 0750 1000 1000 - -"
              "d ${wolfPaths.guest.downloads} 0750 1000 1000 - -"
              "d ${wolfPaths.guest.logs} 0750 1000 1000 - -"
              "d ${wolfPaths.guest.manifests} 0750 1000 1000 - -"
              "d ${wolfPaths.guest.tools} 0750 1000 1000 - -"
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
            || wolf.environment.__EGL_VENDOR_LIBRARY_FILENAMES != expectedEglVendorFiles
            || wolf.environment.HOST_APPS_STATE_FOLDER != "/var/lib/wolf"
            || wolf.environment.LD_LIBRARY_PATH != expectedNvrtcContainerPath
            || wolf.environment.WOLF_PULSE_IMAGE != expectedPulseImage
            || wolf.environment.WOLF_USE_ZERO_COPY != "FALSE"
            || !lib.any (lib.hasInfix "nardol-wolf-config-policy") wolfPreStart
            || !lib.elem "/srv/wolf/data:/var/lib/wolf:rw" wolf.volumes
            || lib.elem "/srv/wolf/config:/etc/wolf:rw" wolf.volumes
            || !lib.elem expectedNvrtcMount wolf.volumes
            || !validateWolfConfig wolfConfig
            || !lib.all (fixture: !(validateWolfConfig fixture)) malformedFixtures
            || !lib.elem "/dev/uinput:/dev/uinput" wolf.devices
            || !lib.elem "/dev/uhid:/dev/uhid" wolf.devices
            || !lib.all (rule: lib.elem rule cfg.systemd.tmpfiles.rules) expectedGamingDirectories
          then
            throw "nardol Wolf image policy, privilege, state, network, NVIDIA EGL/NVRTC/copy-path, or input contract changed"
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
