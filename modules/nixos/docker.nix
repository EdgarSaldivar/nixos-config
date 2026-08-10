{
  virtualisation.docker = {
    enable = true;

    daemon.settings = {
      # The disko-managed SN850X is mounted at /srv. Image layers and build
      # cache are the host's heaviest write load, so keep them on the faster,
      # higher-endurance drive and leave the Samsung root relatively quiet.
      data-root = "/srv/docker";
    };

    autoPrune = {
      enable = true;
      dates = "weekly";
      flags = [ "--filter=until=720h" ];
    };
  };

  # Never let socket activation create Docker's state directory on the Samsung
  # root before the encrypted SN850X mount is available.
  systemd.services.docker.unitConfig.RequiresMountsFor = [ "/srv/docker" ];
}
