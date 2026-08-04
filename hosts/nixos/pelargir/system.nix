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

    # minas.saldivar.io's public record CNAMEs through the friend's domain to a
    # PRIVATE address (site A is double-NATed and publishes its inner WAN IP) —
    # verified 2026-08-03. Every reference to minas from this host (restic sftp,
    # the backup preflight probe, MINAS-PREP validation) must ride wg0 instead,
    # so pin the name to minas' WireGuard address. Remove only if site A ever
    # gains a real public A record.
    hosts."192.168.4.6" = [ "minas.saldivar.io" ];
  };

  time.timeZone = "America/Los_Angeles";
  system.stateVersion = "26.05";
  nixpkgs.config.allowUnfree = true;

  zramSwap = {
    enable = true;
    memoryPercent = 25;
  };

  services.journald.extraConfig = ''
    SystemMaxUse=500M
  '';
  services.fstrim.enable = true;

  services.openssh = {
    enable = true;
    # INSTALL-RUNBOOK step 5 places this pre-generated key before first boot;
    # sops-nix derives the machine age identity from the same stable key.
    hostKeys = [
      {
        path = "/etc/ssh/ssh_host_ed25519_key";
        type = "ed25519";
      }
    ];
    settings = {
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      PermitRootLogin = "no";
    };
  };

  nix.settings.experimental-features = [
    "nix-command"
    "flakes"
  ];
}
