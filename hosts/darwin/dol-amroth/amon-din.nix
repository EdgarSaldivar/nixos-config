# Amon Dîn on dol-amroth.
#
# The packages themselves live in pkgs/amon-din.nix so they can be used without
# nix-darwin — see the note there. This module is only the wiring for a Mac that
# does run it.
{ pkgs, ... }:
let
  amonDin = import ../../../pkgs/amon-din.nix { inherit pkgs; };
in
{
  environment.systemPackages = (builtins.attrValues amonDin) ++ [ pkgs.swiftbar ];

  # Point SwiftBar at the nix-managed plugin directory, or it prompts on first
  # launch and the item silently never appears.
  system.defaults.CustomUserPreferences."com.ameba.SwiftBar" = {
    PluginDirectory = "${amonDin.amon-din-menubar}/bin";
    DisableBashWrapper = true;
  };
}
