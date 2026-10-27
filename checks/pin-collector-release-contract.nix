{
  lib,
  pkgs,
  nixosConfigurations,
  darwinConfigurations,
  ...
}:

# A digest alone identifies bytes, but not the reviewed source revision the
# image claims to contain. Require both published images to carry independently
# recorded full Git revisions that match the one approved for the release.
let
  contract = import ../hosts/nixos/minas-tirith/pin-collector-release-contract.nix {
    inherit lib;
  };
  shaA = lib.concatStrings (lib.replicate 40 "a");
  shaB = lib.concatStrings (lib.replicate 40 "b");
  valid = {
    staged = true;
    gitRevision = shaA;
    apiImageRevision = shaA;
    modelImageRevision = shaA;
  };
  enabled = valid // {
    enabled = true;
  };
  inactive = {
    staged = false;
    gitRevision = null;
    apiImageRevision = null;
    modelImageRevision = null;
  };
  accepts = release: (builtins.tryEval (contract.assertValid release)).success;
in
if
  !accepts valid
  || !accepts inactive
  || accepts (inactive // { gitRevision = "malformed"; })
  || accepts (valid // { gitRevision = builtins.substring 0 39 shaA; })
  || accepts (valid // { apiImageRevision = "A${builtins.substring 1 39 shaA}"; })
  || accepts (valid // { modelImageRevision = builtins.substring 0 39 shaA; })
  || accepts (builtins.removeAttrs valid [ "apiImageRevision" ])
  || accepts (valid // { apiImageRevision = shaB; })
  || accepts (valid // { modelImageRevision = shaB; })
  || !accepts (
    valid
    // {
      notificationSender = true;
      collectorPages = true;
    }
  )
  || accepts (valid // { notificationSender = "yes"; })
  || accepts (valid // { collectorPages = 1; })
  # Stardust and Plus: booleans, off when absent.
  || !accepts (
    valid
    // {
      stardust = true;
      plus = true;
    }
  )
  || !accepts (
    valid
    // {
      stardust = false;
      plus = false;
    }
  )
  || accepts (valid // { stardust = "true"; })
  || accepts (valid // { plus = 1; })
  # The conversion: one of three modes, only in an enabled release.
  || !accepts (enabled // { stardustConversion = "dry-run"; })
  || !accepts (enabled // { stardustConversion = "apply"; })
  || !accepts (enabled // { stardustConversion = "refresh"; })
  || !accepts (enabled // { stardustConversion = null; })
  || accepts (enabled // { stardustConversion = "--apply"; })
  || accepts (enabled // { stardustConversion = "Apply"; })
  || accepts (enabled // { stardustConversion = ""; })
  || accepts (enabled // { stardustConversion = true; })
  || accepts (valid // { stardustConversion = "dry-run"; })
  || accepts (
    enabled
    // {
      enabled = false;
      stardustConversion = "dry-run";
    }
  )
  # A writing conversion once stardust is on would get round the daily limits; a dry run
  # writes nothing.
  || accepts (
    enabled
    // {
      stardust = true;
      stardustConversion = "apply";
    }
  )
  || accepts (
    enabled
    // {
      stardust = true;
      stardustConversion = "refresh";
    }
  )
  || !accepts (
    enabled
    // {
      stardust = true;
      plus = true;
      stardustConversion = "dry-run";
    }
  )
  || !accepts (
    enabled
    // {
      plus = true;
      stardustConversion = "refresh";
    }
  )
then
  throw "PinCollector release revision contract accepted malformed or mismatched evidence"
else
  pkgs.runCommand "pin-collector-release-contract-ok" { } "touch $out"
