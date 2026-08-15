# nardol — unattended LUKS2 unlock through Tang, with manual fallbacks.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  tangUrl = "http://10.0.0.165:7654";
  luksDevices = [
    {
      name = "nardol-root";
      device = "/dev/disk/by-partlabel/nardol-root-luks";
    }
    {
      name = "nardol-fast";
      device = "/dev/disk/by-partlabel/nardol-fast-luks";
    }
  ];
  expectedBinding = "1: tang '{\"url\":\"${tangUrl}\"}'";

  # Nardol travels, so initrd SSH must be reachable from whatever subnet the
  # host lands on: a laptop at a LAN party is on the same L2 but not on
  # 10.0.0.0/24. ProxyJump via pelargir remains HOME-LAN recovery only -- it
  # cannot reach a machine physically at a party.
  #
  # The real controls are key-only auth and the forced password-agent command
  # below; the source restriction is defence in depth, so widening it to RFC1918
  # costs little. The initrd still takes no default route or DNS.
  initrdAuthorizedKeys = map (
    key:
    ''from="10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",command="/bin/systemd-tty-ask-password-agent",no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-user-rc ${key}''
  ) config.users.users.edgar.openssh.authorizedKeys.keys;

  # A dedicated USB stick carries a 4096-byte key at a fixed raw offset, in
  # keyslot 2 on both volumes. This is what unlocks nardol away from home, where
  # Tang is unreachable BY DESIGN -- network-bound encryption is the property
  # worth keeping, which is also why TPM binding was rejected: it would make a
  # stolen machine decrypt itself.
  usbKeyDevice = "/dev/disk/by-id/usb-General_USB_Flash_Disk_0305500000000280-0:0";
  usbKeyOffset = 4194304; # 4 MiB, clear of any partition/metadata region
  usbKeySize = 4096;
  # Load-bearing, not a tuning knob. WITHOUT a timeout systemd generates a HARD
  # dependency on the by-id device, so a missing stick means systemd-cryptsetup
  # never starts: no password request appears, the Clevis askpass watcher has
  # nothing to answer, and the boot hangs with neither console nor SSH recovery.
  # With it the dependency is soft -- the key is tried, discarded, and the normal
  # ask-password path opens. Starting value; confirm by cold-boot measurement.
  usbKeyTimeout = 10;
in
{
  boot.initrd = {
    systemd = {
      enable = true;

      # Match the Intel I211 by its immutable MAC instead of an interface name.
      # 10.0.0.118 must remain reserved/excluded for this MAC at the router; see
      # INSTALL-RUNBOOK.md before enabling the first encrypted boot.
      network = {
        enable = true;
        wait-online.timeout = 20;
        networks."10-nardol-lan" = {
          matchConfig.MACAddress = "9c:6b:00:36:e0:e8";
          # No static address. Nardol travels to LAN parties, where a hard-coded
          # 10.0.0.118/24 is meaningless and may actively collide with a party
          # network that also uses 10.0.0.0/24. The home router RESERVES this
          # address for this MAC, so DHCP still yields 10.0.0.118 at home and
          # Tang's /32 source ACL keeps matching.
          networkConfig = {
            DHCP = "ipv4";
            IPv6AcceptRA = false;
            LinkLocalAddressing = "no";
          };
          # Address ONLY. A foreign DHCP server must not be able to put a
          # default route or resolver into the initrd. UseGateway alone is not
          # enough: it suppresses only the Router option, while UseRoutes
          # defaults true and option-121 classless routes would otherwise be
          # installed anyway. IPv6AcceptRA above closes the router-advertisement
          # path, which neither of those covers.
          dhcpV4Config = {
            UseGateway = false;
            UseRoutes = false;
            UseDNS = false;
            # Makes the home reservation an explicit dependency rather than an
            # accident of client-id defaults.
            ClientIdentifier = "mac";
            # A foreign router's lease table is the only practical way to find
            # nardol off-site; this is what it shows up as.
            Hostname = "nardol-initrd";
          };
          linkConfig.RequiredForOnline = "routable";
        };

        # These catch-all units are not generated while the legacy
        # boot.initrd.network.enable stays false. Keep explicit disabled
        # sentinels so enabling it later cannot silently add initrd DHCP; stage
        # 2 still uses the router reservation.
        networks."99-ethernet-default-dhcp".enable = false;
        networks."99-wireless-client-dhcp".enable = false;
      };

      # The SSH key below is deliberately restricted to the password agent.
      # Make its command path explicit instead of depending on initrd contents.
      extraBin.systemd-tty-ask-password-agent = "${config.boot.initrd.systemd.package}/bin/systemd-tty-ask-password-agent";
    };

    network.ssh = {
      enable = true;
      port = 2222;

      # This private key is copied into the unencrypted initrd/ESP. It must be
      # a dedicated throwaway host key, never nardol's stage-2 SSH host key.
      # Generate and ship it at install time as documented in the runbook.
      hostKeys = [ "/etc/secrets/initrd/ssh_host_ed25519_key" ];
      authorizedKeys = initrdAuthorizedKeys;
      extraConfig = ''
        AllowUsers root
        AllowTcpForwarding no
        GatewayPorts no
        PermitTunnel no
        X11Forwarding no
        PasswordAuthentication no
        KbdInteractiveAuthentication no
        AuthenticationMethods publickey
      '';
    };

    # This watches systemd's LUKS password request and repeatedly tries the
    # Clevis token while the ordinary passphrase prompt remains active. If Tang
    # is absent or the token fails, console and initrd SSH still work.
    clevisLuksAskpass = {
      enable = true;
      useTang = true;
    };
  };

  # services.lvm must remain enabled because its device-mapper udev rules are
  # required by cryptsetup. Nardol itself has no LVM PVs, however, and the
  # preserved Crucial still contains a foreign Proxmox "pve" VG. Reject all PV
  # scanning in both boot stages so that old VG stays inert and cannot create
  # transient activation failures (or expose its many thin volumes).
  environment.etc."lvm/lvm.conf".text = lib.mkAfter ''
    devices/global_filter = [ "r|.*|" ]
  '';
  boot.initrd.systemd.contents."/etc/lvm/lvm.conf".text = ''
    config {}
    devices/global_filter = [ "r|.*|" ]
  '';

  # Merge the USB keyfile onto the devices disko declares. fallbackToPassword is
  # deliberately NOT set: systemd stage 1 implies it, and nixpkgs asserts it must
  # stay false (luksroot.nix, "fallbackToPassword is implied by systemd stage 1").
  boot.initrd.luks.devices = lib.genAttrs (map (luks: luks.name) luksDevices) (_: {
    keyFile = usbKeyDevice;
    keyFileSize = usbKeySize;
    keyFileOffset = usbKeyOffset;
    keyFileTimeout = usbKeyTimeout;
  });

  environment.systemPackages = with pkgs; [
    clevis
    cryptsetup
  ];

  # dm-crypt passes trims because disko explicitly accepts the allocation-
  # pattern tradeoff. Periodic trim avoids continuous-discard latency.
  services.fstrim.enable = true;

  # Fail visibly after boot if installation omitted either enrollment, selected
  # a different slot, or bound to the wrong Tang URL. Manual LUKS unlock still
  # permits repair; this check does not hold the boot hostage.
  systemd.services.nardol-clevis-binding-check = {
    description = "Verify Nardol LUKS volumes have the expected Tang bindings";
    wantedBy = [ "multi-user.target" ];
    after = [ "local-fs.target" ];
    serviceConfig.Type = "oneshot";
    script = ''
      expected=${lib.escapeShellArg expectedBinding}

      ${lib.concatMapStringsSep "\n" (luks: ''
        binding="$(${pkgs.clevis}/bin/clevis luks list -d ${lib.escapeShellArg luks.device})"
        if [ "$binding" != "$expected" ]; then
          echo "${luks.name}: expected Clevis binding: $expected" >&2
          echo "${luks.name}: actual Clevis bindings: $binding" >&2
          exit 1
        fi
      '') luksDevices}
    '';
  };

  assertions = [
    {
      assertion = config.boot.initrd.clevisLuksAskpass.enable -> config.boot.initrd.systemd.enable;
      message = "nardol: Clevis askpass requires the systemd initrd.";
    }
    {
      assertion =
        config.boot.initrd.clevisLuksAskpass.useTang -> config.boot.initrd.systemd.network.enable;
      message = "nardol: Tang unlock requires systemd-networkd in the initrd.";
    }
    {
      assertion = lib.elem "igb" config.boot.initrd.availableKernelModules;
      message = "nardol: initrd unlock requires the Intel I211 igb driver in the initrd.";
    }
    {
      assertion = initrdAuthorizedKeys != [ ];
      message = "nardol: initrd SSH requires at least one Edgar authorized key.";
    }
  ];
}
