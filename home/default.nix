{ pkgs, lib, dotfiles, ... }: {
  imports = [
    #./profiles/git.nix
    #./profiles/fish.nix
    #./editor/vim.nix
  ];

  home = {
    stateVersion = "24.05";
  };
}
