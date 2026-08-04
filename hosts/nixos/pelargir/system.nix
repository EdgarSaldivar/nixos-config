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
