{ ... }: {
  programs.git = {
    enable = true;
    userName = "Edgar Saldivar";
    userEmail = "edgar@saldivar.io";
    extraConfig = {
      core = {
        editor = "vim";
      };
      pull = {
        rebase = true;
      };
    };
    ignores = [ ".DS_Store" ];
  };
}
