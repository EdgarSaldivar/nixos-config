{
  lib,
  pkgs,
  nixosConfigurations,
  darwinConfigurations,
  ...
}:

# A rotated secret only reaches the cluster if k3s-apply-secrets runs again.
# It is a `oneshot` with `RemainAfterExit`, so activation skips it unless
# something names it: changing a VALUE does not change the unit definition.
# The failure is silent and looks like success -- the rebuild passes, sops
# reports the new value, and the cluster keeps serving the old one. That
# stranded a broken GHCR credential for two hours on 2026-08-15.
# The trap belongs to the applier, not to PinCollector, so cover everything it
# pushes: the pin_collector_* files and the three rendered Secret templates
# (home, cluster-apps, authentik). Rotating a nextcloud or authentik value hits
# exactly the same silent skip.
let
  pelargir = nixosConfigurations.pelargir.config;
  applied = "k3s-apply-secrets.service";
  script = pelargir.systemd.services.k3s-apply-secrets.script;
  # Derive what to protect from the applier ITSELF rather than from a name
  # pattern. Anything whose rendered path the script reads is pushed into the
  # cluster, so a future secret is covered no matter what it is called --
  # matching on a prefix would quietly exempt anything named differently.
  candidates =
    lib.mapAttrs' (n: v: lib.nameValuePair "secret ${n}" v) pelargir.sops.secrets
    // lib.mapAttrs' (n: v: lib.nameValuePair "template ${n}" v) pelargir.sops.templates;
  referenced = lib.filterAttrs (_: entry: lib.hasInfix entry.path script) candidates;
  missing = lib.attrNames (
    lib.filterAttrs (_: entry: !(lib.elem applied (entry.restartUnits or [ ]))) referenced
  );
in
# Guard the vacuous pass: if the script stops interpolating sops paths, this
# check would otherwise succeed by protecting nothing at all.
if referenced == { } then
  throw "${applied} references no sops paths; this contract would pass vacuously"
else if missing != [ ] then
  throw "these must restart ${applied}: ${lib.concatStringsSep ", " missing}"
else
  pkgs.runCommand "secret-applier-contract-ok" { } "touch $out"
