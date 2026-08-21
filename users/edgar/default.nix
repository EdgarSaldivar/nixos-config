# Dropped the unused `utils` and `sops` module arguments: neither is supplied
# by the module system here, so any host importing this failed to evaluate
# with "called without required argument 'sops'".
{
  config,
  lib,
  pkgs,
  ...
}:
{
  # ---------------------------------------------------------------------------
  # Shells — fleet-wide, and the split is deliberate.
  # ---------------------------------------------------------------------------
  # root -> bash. NOT NEGOTIABLE, and not merely the default: this is the shell
  # sulogin hands you in stage-2 emergency, which is the exact remote-recovery
  # path this fleet has been hardened around. Every recovery snippet, every man
  # page example, every answer you will paste at 2am is POSIX. A non-POSIX root
  # shell turns a recoverable situation into a translation exercise. Stated
  # explicitly so it cannot drift silently if a default changes upstream.
  #
  # edgar -> bash AS THE LOGIN SHELL, execing fish for interactive sessions.
  # See ./home.nix — the full reasoning lives there, but the short version is
  # that `ssh host 'cmd'` runs the LOGIN SHELL with -c, so the login shell is the
  # parser for every remote command, rsync, legacy scp and
  # `nixos-rebuild --target-host`. Fish as login shell breaks those; zsh as login
  # shell merely hides the breakage (zsh -c is not bash -c either). Bash owns the
  # machine-facing path, fish owns the human-facing one, and a broken fish is
  # always repairable remotely because remote commands never enter it.
  #
  # NOTE: the live openSUSE box ran fish as the LOGIN shell for both accounts —
  # that is precisely the configuration this replaces.
  programs.fish.enable = true;

  users = {
    mutableUsers = false;
    users.root.shell = pkgs.bashInteractive;
    users.edgar = {
      isNormalUser = true;
      home = "/home/edgar";
      description = "Edgar Saldivar";
      shell = pkgs.bashInteractive;
      # `networkmanager` only exists as a group when NetworkManager is enabled.
      # minas-tirith uses systemd-networkd, so unconditionally listing it made
      # user activation complain about a group that does not exist on that host.
      extraGroups = [
        "wheel"
      ]
      ++ lib.optional config.networking.networkmanager.enable "networkmanager";
      openssh.authorizedKeys.keys = [
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA+PoI3q67ZKz5oWtHVWfKzIRyBagoaFqYu/TqndfqTW"
      ];
    };
  };

  # useGlobalPkgs makes home-manager use the system nixpkgs rather than its own
  # instance — one package set, no silent divergence between what the system and
  # the user shell are built against.
  home-manager = {
    useGlobalPkgs = true;
    useUserPackages = true;
    users.edgar = import ./home.nix;
  };
}
