{ buildGoModule, lib }:
buildGoModule {
  pname = "nardol-gateway";
  version = "0.1.0";
  src = ./.;
  # Standard library only — nothing to vendor, and deliberately so. This sits
  # in the path of every voice command; a dependency tree is a liability here.
  vendorHash = null;
  meta = {
    description = "Wake-on-demand reverse proxy for a sleeping inference host";
    mainProgram = "nardol-gateway";
    platforms = lib.platforms.linux;
  };
}
