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
  # edgar -> zsh. See ./home.nix for the reasoning; briefly, it keeps one shell
  # across the fleet AND macOS, stays POSIX so `ssh host '<snippet>'` works, and
  # with the home-manager plugins reaches rough parity with fish interactively.
  #
  # NOTE: the live openSUSE box ran fish for BOTH accounts. That is the state
  # this replaces — it is a deliberate migration, not an oversight. Edgar's fish
  # config is preserved in the backup at
  # /storage2/backup-2026-07-30/home/edgar/.config/fish if it is ever wanted.
  programs.zsh.enable = true;

  users = {
    mutableUsers = false;
    users.root.shell = pkgs.bashInteractive;
    users.edgar = {
      isNormalUser = true;
      home = "/home/edgar";
      description = "Edgar Saldivar";
      shell = pkgs.zsh;
      # `networkmanager` only exists as a group when NetworkManager is enabled.
      # minas-tirith uses systemd-networkd, so unconditionally listing it made
      # user activation complain about a group that does not exist on that host.
      extraGroups = [
        "wheel"
      ] ++ lib.optional config.networking.networkmanager.enable "networkmanager";
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
