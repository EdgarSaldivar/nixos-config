{ pkgs, config, ... }: {
  # Runs scripts after install
  
  system.activationScripts.move-age = {
    text = ''
      ${pkgs.bash}/bin/bash ${../../scripts/move-age.sh}
    '';
  };
}