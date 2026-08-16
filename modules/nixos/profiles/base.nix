# Baseline shared by every NixOS host.
{ pkgs, ... }:
{
  nix.settings = {
    experimental-features = [
      "nix-command"
      "flakes"
    ];

    # cuda-maintainers spares you building CUDA closures from source, which
    # is the difference between minutes and hours on an inference box.
    substituters = [
      "https://cache.nixos.org"
      "https://cuda-maintainers.cachix.org"
    ];
    trusted-public-keys = [
      "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
      "cuda-maintainers.cachix.org-1:0dq3bujKpuEPMCX6U4WylrUDZ9JyUG0VpVZa7CNfq5E="
    ];
  };

  # This fleet is flake-only; imperative channels leave an accidental non-reproducible path.
  nix.channel.enable = false;

  # Without these the store grows without bound; CUDA closures are enormous.
  nix.gc = {
    automatic = true;
    dates = "weekly";
    options = "--delete-older-than 30d";
  };
  nix.optimise.automatic = true;

  nixpkgs.config.allowUnfree = true;

  time.timeZone = "America/Los_Angeles";
  i18n.defaultLocale = "en_US.UTF-8";

  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      PermitRootLogin = "no";
    };
  };

  environment.systemPackages = with pkgs; [
    curl
    git
    lsof
    pciutils
    vim
    wget
  ];
}
