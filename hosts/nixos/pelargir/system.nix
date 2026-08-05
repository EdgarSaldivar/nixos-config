# pelargir — identity, base networking, and bounded local state.
{ ... }:
{
  networking = {
    hostName = "pelargir";
    hostId = "54487bae";

    # eth0 receives its stable LAN address from the router reservation. Keeping
    # DHCP here makes rescue/replacement routers usable without editing the host.
    useDHCP = false;
    interfaces.eth0.useDHCP = true;

    # MagicDNS now resolves fleet peers by tailnet short name; no static host
    # pin is needed for minas-tirith.
  };

  system.stateVersion = "26.05";

  # ---------------------------------------------------------------------------
  # Administrability — learned the hard way on 2026-08-04.
  # ---------------------------------------------------------------------------
  # The first install of this host booted perfectly and was then IMPOSSIBLE to
  # administer. users/edgar/default.nix sets mutableUsers = false, this host set
  # no password, sshd has PermitRootLogin = "no", and NixOS defaults
  # security.sudo.wheelNeedsPassword to true. So sudo prompted for a password
  # that did not exist, the console login prompt could not be satisfied either,
  # and recovery meant physically unseating the NVMe so the rescue card would
  # boot. minas-tirith/secrets.nix documents this exact trap at length; pelargir
  # walked into it anyway, because the Pi-4-era config that DID set
  # wheelNeedsPassword was deleted wholesale in the rewrite.
  #
  # Two independent ways in, on purpose. Either alone is a single point of
  # failure for administration:
  #   1. passwordless sudo for wheel — remote admin over ssh keys
  #   2. a real console password (see ./secrets.nix) — a keyboard or serial
  #      console still works when sshd, networking or the tailnet is broken
  security.sudo = {
    enable = true;
    wheelNeedsPassword = false;
  };

  zramSwap = {
    enable = true;
    memoryPercent = 25;
  };

  services.journald.extraConfig = ''
    SystemMaxUse=500M
  '';
  services.fstrim.enable = true;

  services.openssh = {
    # INSTALL-RUNBOOK step 7 places this pre-generated key before first boot;
    # sops-nix derives the machine age identity from the same stable key.
    hostKeys = [
      {
        path = "/etc/ssh/ssh_host_ed25519_key";
        type = "ed25519";
      }
    ];
    settings = {
      KbdInteractiveAuthentication = false;
    };
  };
}
