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

  # The top-level key names of secrets/pin-collector.yaml. SOPS encrypts only the values, so the
  # names read in clear; no value is looked at.
  pinCollectorSecretNames = map (line: lib.head (lib.splitString ":" line)) (
    lib.filter (line: builtins.match "[a-z0-9_]+:.*" line != null) (
      lib.splitString "\n" (builtins.readFile ../../../secrets/pin-collector.yaml)
    )
  );
  hasPinCollectorSecret = name: builtins.elem name pinCollectorSecretNames;
in
{
  inherit isFullGitRevision revisionContractSatisfied hasPinCollectorSecret;

  # The pinned release only (pelargir's secrets/sops.nix, which declares these secrets): a gate whose
  # secret isn't in secrets/pin-collector.yaml yet fails evaluation, and so nix flake check, naming
  # the key, rather than sops-nix failing pelargir's build later. Renders in checks/ go through
  # assertValid alone, so they can show a gate's on state before its secret exists.
  assertSecretsPresent =
    release:
    assert lib.assertMsg (
      !(release.photoScreeningKey or false) || hasPinCollectorSecret "openai_api_key"
    ) "PinCollector photoScreeningKey needs openai_api_key in secrets/pin-collector.yaml";
    assert lib.assertMsg
      (!(release.peopleReportsAlert or false) || hasPinCollectorSecret "people_reports_alert_url")
      "PinCollector peopleReportsAlert needs people_reports_alert_url (the Healthchecks ping URL) in secrets/pin-collector.yaml";
    release;

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
    # Suggest details' outside lookups (PinCollector Step 3): SerpApi's, SearchApi's and the
    # reader's keys, declared, applied and mounted only once secrets/pin-collector.yaml holds all three.
    assert lib.assertMsg (builtins.isBool (
      release.suggestDetailsKeys or false
    )) "PinCollector suggestDetailsKeys must be a boolean";
    # Account mail (PinCollector's password reset codes): Resend's key, declared, applied and mounted
    # into the API only once secrets/pin-collector.yaml holds resend_api_key.
    assert lib.assertMsg (builtins.isBool (
      release.accountMail or false
    )) "PinCollector accountMail must be a boolean";
    # Sign in with Apple's private key (.p8) and its Key ID, so the server can keep Apple refresh tokens
    # and revoke them when an account is deleted. Off until secrets/pin-collector.yaml holds
    # apple_sign_in_key; on, it needs the key's Key ID (ten capitals and digits) beside it.
    assert lib.assertMsg (builtins.isBool (
      release.appleSignInKey or false
    )) "PinCollector appleSignInKey must be a boolean";
    assert lib.assertMsg (
      !(release.appleSignInKey or false)
      || (
        builtins.isString (release.appleKeyId or null)
        && builtins.match "[A-Z0-9]{10}" release.appleKeyId != null
      )
    ) "PinCollector appleSignInKey needs appleKeyId, the Sign in with Apple key's 10-character Key ID";
    # Photo screening (PinCollector #290): the OpenAI key, the switch that checks every new catalog
    # photo with it, and the People reports alert's ping URL. The switch needs the key; each key's
    # secret must be in secrets/pin-collector.yaml (assertSecretsPresent).
    assert lib.assertMsg (builtins.isBool (
      release.photoScreeningKey or false
    )) "PinCollector photoScreeningKey must be a boolean";
    assert lib.assertMsg (builtins.isBool (
      release.photoChecks or false
    )) "PinCollector photoChecks must be a boolean";
    assert lib.assertMsg (
      !(release.photoChecks or false) || (release.photoScreeningKey or false)
    ) "PinCollector photoChecks needs photoScreeningKey, which mounts the OpenAI key";
    assert lib.assertMsg (builtins.isBool (
      release.peopleReportsAlert or false
    )) "PinCollector peopleReportsAlert must be a boolean";
    # The nightly release import mounts the reader's key, so it needs the release that declares it.
    assert lib.assertMsg (builtins.isBool (
      release.releaseImport or false
    )) "PinCollector releaseImport must be a boolean";
    assert lib.assertMsg (
      !(release.releaseImport or false) || (release.suggestDetailsKeys or false)
    ) "PinCollector releaseImport needs suggestDetailsKeys, which provides the reader's key";
    # The news import mounts the reader's key too, so it needs the same release.
    assert lib.assertMsg (builtins.isBool (
      release.newsImport or false
    )) "PinCollector newsImport must be a boolean";
    assert lib.assertMsg (
      !(release.newsImport or false) || (release.suggestDetailsKeys or false)
    ) "PinCollector newsImport needs suggestDetailsKeys, which provides the reader's key";
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
