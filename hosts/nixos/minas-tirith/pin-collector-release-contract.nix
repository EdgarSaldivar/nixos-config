{ lib }:
let
  isFullGitRevision = value: builtins.isString value && builtins.match "[0-9a-f]{40}" value != null;

  revisionContractSatisfied =
    release:
    let
      gitRevision = release.gitRevision or null;
      apiImageRevision = release.apiImageRevision or null;
      modelImageRevision = release.modelImageRevision or null;
    in
    (gitRevision == null && apiImageRevision == null && modelImageRevision == null)
    || (
      isFullGitRevision gitRevision
      && isFullGitRevision apiImageRevision
      && isFullGitRevision modelImageRevision
      && apiImageRevision == gitRevision
      && modelImageRevision == gitRevision
    );
in
{
  inherit isFullGitRevision revisionContractSatisfied;

  assertValid =
    release:
    assert lib.assertMsg (
      !release.staged || isFullGitRevision (release.gitRevision or null)
    ) "A staged PinCollector release must declare its reviewed full lowercase Git revision";
    assert lib.assertMsg (revisionContractSatisfied release)
      "PinCollector API and model OCI revisions must be full Git SHAs matching the reviewed revision";
    assert lib.assertMsg (builtins.isBool (
      release.apiMaintenance or false
    )) "PinCollector apiMaintenance must be a boolean";
    # Feature gates for config an older image cannot use. Each is optional and off by
    # default, and is turned on in the release that first ships an image carrying it.
    assert lib.assertMsg (builtins.isBool (
      release.notificationSender or false
    )) "PinCollector notificationSender must be a boolean";
    assert lib.assertMsg (builtins.isBool (
      release.collectorPages or false
    )) "PinCollector collectorPages must be a boolean";
    assert lib.assertMsg (builtins.isBool (
      release.stardust or false
    )) "PinCollector stardust must be a boolean";
    assert lib.assertMsg (builtins.isBool (
      release.plus or false
    )) "PinCollector plus must be a boolean";
    # The one-time stardust conversion Job (docs/runbooks/minas-tirith/pin-collector.md).
    assert lib.assertMsg (builtins.elem (release.stardustConversion or null) [
      null
      "dry-run"
      "apply"
      "refresh"
    ]) ''PinCollector stardustConversion must be "dry-run", "apply" or "refresh" when set'';
    assert lib.assertMsg (
      (release.stardustConversion or null) == null || (release.enabled or false) == true
    ) "PinCollector stardustConversion needs an enabled release";
    # Once stardust is on, normal settles pay new history under the daily limits, and a
    # conversion would get round them. The command refuses then too; this fails earlier.
    assert lib.assertMsg (
      !(release.stardust or false)
      || !(builtins.elem (release.stardustConversion or null) [
        "apply"
        "refresh"
      ])
    ) "PinCollector stardustConversion apply or refresh must run before stardust is on";
    release;
}
