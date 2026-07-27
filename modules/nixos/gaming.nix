{
  programs.steam = {
    enable = true;
    remotePlay.openFirewall = false;
  };

  # Wolf (games-on-whales) runs as a container rather than a NixOS service.
  # It needs uinput plus udev rules for the virtual input devices it creates;
  # that is the remaining unproven piece of the migration.
  boot.kernelModules = [ "uinput" ];
  hardware.uinput.enable = true;
}
