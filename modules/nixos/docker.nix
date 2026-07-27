{
  virtualisation.docker = {
    enable = true;

    daemon.settings = {
      # TODO(nardol): once Ubuntu is retired and the SN850X is repurposed,
      # point this at the SN850X. Image layers and build cache are by far the
      # heaviest write load on this machine (~214GB of build cache observed),
      # and the SN850X has 2400 TBW against the P3 Plus's 800.
      #
      # data-root = "/mnt/fast/docker";
    };

    autoPrune = {
      enable = true;
      dates = "weekly";
      flags = [ "--filter=until=720h" ];
    };
  };
}
