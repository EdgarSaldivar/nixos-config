# Validated only on nardol: this is a single-consumer role bundle named for the
# game-streaming role, not a general Docker capability. Enabling Docker itself
# is reusable, but /srv/docker and RequiresMountsFor assume nardol's layout,
# where /srv is a directory on its encrypted root. Before a second host imports this module, parameterise
# those paths and mount assumptions and decide whether the role's auto-pruning
# policy is suitable there.

{
  virtualisation.docker = {
    enable = true;

    daemon.settings = {
      # /srv is a directory on nardol's single encrypted root. Image layers and
      # build cache are the host's heaviest write load and live with the rest
      # of its data under /srv.
      data-root = "/srv/docker";
    };

    autoPrune = {
      enable = true;
      dates = "weekly";
      flags = [ "--filter=until=720h" ];
    };
  };

  # The contract in checks/nardol-gaming-contract.nix keeps this ordering, which
  # resolves to the root mount now that /srv is a plain directory.
  systemd.services.docker.unitConfig.RequiresMountsFor = [ "/srv/docker" ];
}
