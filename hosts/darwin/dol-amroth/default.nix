{ ... }:
{
  imports = [ ./system.nix ];

  # This is the same Home Manager profile used by the NixOS hosts. In
  # particular, it makes programs.nh available on the Mac where remote fleet
  # builds are normally initiated.
  home-manager = {
    useGlobalPkgs = true;
    useUserPackages = true;
    users.edgar = import ../../../users/edgar/home.nix;
  };
}
