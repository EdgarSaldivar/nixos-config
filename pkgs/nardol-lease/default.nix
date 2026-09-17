{ buildGoModule, lib }:
buildGoModule {
  pname = "nardol-lease";
  version = "0.1.0";
  src = ./.;
  vendorHash = null; # stdlib only, deliberately
  meta = {
    description = "Admission lease that makes suspend impossible while held";
    mainProgram = "nardol-lease";
    platforms = lib.platforms.linux;
  };
}
