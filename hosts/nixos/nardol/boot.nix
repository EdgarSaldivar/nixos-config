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

  # Initrd SSH is reachable only on the local /24. Off-site access is through
  # ProxyJump on pelargir, so nardol's initrd needs no default route or DNS.
  initrdAuthorizedKeys = map (
    key:
    ''from="10.0.0.0/24",command="/bin/systemd-tty-ask-password-agent",no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-user-rc ${key}''
  ) config.users.users.edgar.openssh.authorizedKeys.keys;
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
          address = [ "10.0.0.118/24" ];
          networkConfig = {
            DHCP = "no";
            IPv6AcceptRA = false;
            LinkLocalAddressing = "no";
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
