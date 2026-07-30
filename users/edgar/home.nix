# Edgar's interactive shell — one definition, every host in the fleet.
#
# The point of putting this in home-manager rather than configuring each box is
# that adding a machine should cost nothing: the shell, prompt, history and
# completions are identical on minas-tirith, nardol and anything added later,
# and they are reproducible from git rather than from whatever that host
# happened to accumulate.
#
# zsh rather than fish, deliberately. fish is nicer out of the box — better
# completions, no plugins needed — but it is not POSIX, and on a fleet that
# matters in two concrete ways that came up while building this host:
#   - `ssh host '<snippet>'` runs under the LOGIN shell. With fish, ordinary
#     bash constructs fail; this repeatedly broke automation against the old box
#     and had to be worked around with `bash -s` wrappers.
#   - Runbook and documentation snippets are POSIX. At 2am during a recovery you
#     want paste-and-run, not translation.
# zsh with the plugins below closes nearly all of fish's interactive advantage
# while staying compatible, and it is already the macOS default — so this is one
# shell across the whole fleet instead of two.
#
# root stays on bash — see ../default.nix for why that one is not negotiable.
{ pkgs, ... }:
{
  home.stateVersion = "26.05";

  programs.zsh = {
    enable = true;
    enableCompletion = true;
    autosuggestion.enable = true; # fish-style inline suggestion from history
    syntaxHighlighting.enable = true; # fish-style command validity colouring

    history = {
      size = 100000;
      save = 100000;
      extended = true; # record timestamps
      ignoreDups = true;
      ignoreSpace = true; # leading space keeps a command out of history
      share = true; # history shared live across concurrent sessions
    };

    shellAliases = {
      # These boxes are remote and ZFS-heavy; the things worth one keystroke.
      zst = "zpool status";
      zls = "zfs list -o name,used,avail,refer,mountpoint";
      dps = "docker ps --format 'table {{.Names}}\\t{{.Status}}'";
      jc = "journalctl -xe --no-pager";
      # Guardrails, not laziness: this hardware has already lost a filesystem.
      rm = "rm -i";
      cp = "cp -i";
      mv = "mv -i";
    };

    initContent = ''
      # Comprehensive completion, matching what fish gives for free.
      zstyle ':completion:*' menu select
      zstyle ':completion:*' matcher-list 'm:{a-zA-Z}={A-Za-z}'
      setopt HIST_VERIFY          # expand !! before running, don't just fire it
      setopt INTERACTIVE_COMMENTS # allow # comments when pasting runbook blocks
    '';
  };

  # Prompt. starship works identically under bash/zsh/fish, so if any host or
  # user ends up on a different shell the prompt stays the same.
  programs.starship = {
    enable = true;
    enableZshIntegration = true;
    settings = {
      add_newline = false;
      # These machines are remote and easy to confuse with each other. Always
      # show which host you are on and whether you are root — running the wrong
      # destructive command on the wrong box is the mistake worth designing out.
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
    enableZshIntegration = true; # Ctrl-R history search, Ctrl-T file search
  };

  programs.zoxide = {
    enable = true;
    enableZshIntegration = true; # `z <dir>` frecency jumping
  };
}
