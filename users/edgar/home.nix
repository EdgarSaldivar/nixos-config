# Edgar's interactive shell — one definition, every host in the fleet.
#
# ============================================================================
#  THE LOGIN SHELL IS BASH. THE INTERACTIVE SHELL IS FISH. THIS IS DELIBERATE.
# ============================================================================
#
# `ssh host 'cmd'` does not run bash — OpenSSH runs the account's LOGIN SHELL
# with -c. So the login shell is not a cosmetic preference: it is the parser for
# every remote command, every `rsync host:path`, every legacy `scp -O`, and
# `nixos-rebuild --target-host`. Making fish the login shell makes fish that
# parser, which is why ordinary bash snippets failed against the old box
# (`fish: Missing end to balance this for loop`) and needed `bash -s` wrappers.
#
# An earlier version of this file used zsh as the login shell to "fix" that.
# That reasoning was wrong: zsh -c is not bash -c either. It merely overlaps with
# bash enough to hide the problem until a snippet uses something zsh does not
# share. Swapping one incompatible parser for a less-obviously-incompatible one
# is not a fix.
#
# So: bash owns the machine-facing path, fish owns the human-facing one.
#   - `ssh host 'cmd'`  -> bash -c, NON-INTERACTIVE, guard below declines -> bash
#   - `ssh host`        -> interactive login bash -> execs fish
#   - scp/rsync/sftp    -> non-interactive path -> never touches fish
#
# This is also why fish must NOT be the login shell even though it is the nicer
# shell: fish reads config.fish even non-interactively, and fish's own FAQ
# documents ssh/scp/rsync corruption caused by startup output.
#
# ESCAPE HATCH — if fish is ever broken, this still gets you a working login:
#     ssh -t host 'BASH_NO_FISH=1 bash -il'
# Remote commands never enter fish, so a broken fish config is always repairable
# remotely. That property is why this is safe on a machine an hour away.
#
# root stays on bash entirely — see ../default.nix.
{ pkgs, ... }:
{
  home.stateVersion = "26.05";

  # nh belongs in Home Manager, not modules/nixos/common.nix: this profile is
  # shared by Edgar's NixOS accounts and, through dol-amroth's nix-darwin
  # configuration, the Mac that drives remote fleet rebuilds. Putting it only
  # in the NixOS baseline would omit the more important Mac-side workflow.
  programs.nh = {
    enable = true;
    flake = "~/Development/nixos-config";

    # Keep exactly one scheduled garbage collector. The fleet baseline already
    # runs nix.gc weekly with 30-day retention, so nh is only the interactive
    # rebuild/GC front-end; enabling nh.clean as well would create two timers.
    clean.enable = false;
  };

  home.sessionVariables.NH_SHOW_ACTIVATION_LOGS = "true";

  programs.bash = {
    enable = true;
    # initExtra, NOT bashrcExtra. home-manager emits its own
    # `[[ $- == *i* ]] || return` guard BEFORE initExtra, so this block only
    # runs in interactive shells. bashrcExtra lands BEFORE that guard and would
    # exec fish during `ssh host 'cmd'` — the exact breakage this design exists
    # to prevent. The explicit $- test below is redundant belt-and-braces, kept
    # so a change in home-manager's ordering cannot silently strand the fleet.
    initExtra = ''
      if [[ $- == *i* ]] \
         && [[ -z "''${BASH_NO_FISH:-}" ]] \
         && [[ -z "''${INSIDE_EMACS:-}" ]] \
         && command -v fish >/dev/null 2>&1; then
        exec fish
      fi
    '';
  };

  programs.fish = {
    enable = true;
    shellAliases = {
      zst = "zpool status";
      zls = "zfs list -o name,used,avail,refer,mountpoint";
      dps = "docker ps --format 'table {{.Names}}\\t{{.Status}}'";
      jc = "journalctl -xe --no-pager";
      # Guardrails, not laziness: this hardware has already lost a filesystem.
      rm = "rm -i";
      cp = "cp -i";
      mv = "mv -i";
    };
    interactiveShellInit = ''
      set -g fish_greeting            # no banner
    '';
  };

  # starship rather than tide: it renders identically under bash, fish and zsh,
  # so the prompt stays the same on the bash escape-hatch path and on macOS.
  # (The old box used tide; that config is preserved in the backup at
  # /storage2/backup-2026-07-30/home/edgar/.config/fish if it is ever wanted.)
  programs.starship = {
    enable = true;
    enableBashIntegration = true;
    enableFishIntegration = true;
    settings = {
      add_newline = false;
      # These machines are remote and easy to confuse with one another. Always
      # show host and user — running a destructive command on the wrong box is
      # the mistake worth designing out.
      hostname = {
        ssh_only = false;
        format = "[$hostname](bold red) ";
      };
      username = {
        show_always = true;
        format = "[$user]($style)@";
      };
    };
  };

  programs.fzf = {
    enable = true;
    enableBashIntegration = true;
    enableFishIntegration = true;
  };

  programs.zoxide = {
    enable = true;
    enableBashIntegration = true;
    enableFishIntegration = true;
  };
}
