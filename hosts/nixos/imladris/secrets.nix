# imladris — sops-nix wiring. Secret values exist only in /run, never the store.
#
# ✅ IMPORTED 2026-09-11. This file spent the host's early life as a commented-out
# import in ./default.nix, deliberately: sops-nix derives the age identity from
# /etc/ssh/ssh_host_ed25519_key, so secrets/imladris.yaml could not be encrypted
# to a recipient that did not yet exist, and referencing a missing sops file
# fails Nix path resolution at EVALUATION time — breaking `nix flake check` for
# the whole repository, not just this host.
#
# The recipient was derived from the key imladris generated during its own
# install rather than one pre-generated on the Mac, so no copy of the private
# key exists off-host. If the microSD dies, this file must be re-encrypted to a
# new recipient. Tolerable only because all three values are reissuable.
{ config, lib, ... }:
{
  sops = {
    defaultSopsFile = ../../../secrets/imladris.yaml;
    defaultSopsFormat = "yaml";
    age = {
      sshKeyPaths = [ "/etc/ssh/ssh_host_ed25519_key" ];
      generateKey = false;
    };

    secrets = {
      # The second way in. See ./system.nix for why one is not enough.
      #
      # ⛔ neededForUsers IS LOAD-BEARING, and its absence fails quietly.
      #
      # Without it sops decrypts into /run/secrets during the normal activation
      # step, which runs AFTER the `users` activation script. That script reads
      # hashedPasswordFile, finds nothing, warns
      #   warning: password file '/run/secrets/edgar_password_hash' does not exist
      # on a line drowned in bootloader output, and leaves edgar's /etc/shadow
      # entry as `!` — locked. The deploy still exits 0 and sops still reports
      # the secret installed, so everything looks finished while the console
      # login this file exists to provide does not work. Measured here on
      # 2026-09-11, on the very first activation after the import landed.
      #
      # neededForUsers makes sops decrypt to /run/secrets-for-users before user
      # creation. Every other host in the fleet already does this — osgiliath,
      # minas-tirith and pelargir — and imladris was the odd one out.
      edgar_password_hash.neededForUsers = true;

      # Samba keeps its own password database, independent of the system user's
      # password — `smbpasswd` does not read /etc/shadow. This is the value the
      # commissioning step feeds to `smbpasswd -s`. Restart the applier when the
      # value changes: it is a RemainAfterExit oneshot, so without this a rotated
      # secret lands in /run/secrets while Samba keeps accepting the old password.
      samba_password.restartUnits = [ "imladris-samba-password.service" ];

      # For the restic push to minas-tirith. Declared ahead of use so the
      # repository password is generated once, at commissioning, and backed up
      # off-host with everything else — rather than being invented later in a
      # hurry when the backup job is finally wired up.
      #
      # ⚠️ That job is NOT configured yet, and the archive is therefore NOT
      # backed up. It is blocked on capacity: minas-tirith's `storage` pool was
      # ~91% full as of 2026-09-11. Until a restore has actually been performed,
      # every file on this host exists in exactly one place.
      restic_password = { };

    }
    //
      lib.optionalAttrs
        (config.services.terracomputeOps.enable && config.services.terracomputeOps.collector.enable)
        {
          # Materialize each encrypted value only with its consuming role. This
          # keeps staged commissioning from exposing credentials for disabled units.
          terracompute-ssh-identity.restartUnits = [ "terracompute-collector.service" ];
          terracompute-known-hosts.restartUnits = [
            "terracompute-collector.service"
          ]
          ++ lib.optionals config.services.terracomputeOps.actions.enable [
            "terracompute-actions.service"
          ];
          terracompute-vast-read-api-key.restartUnits = [ "terracompute-collector.service" ];
          terracompute-bmc-password.restartUnits = [ "terracompute-collector.service" ];
        }
    //
      lib.optionalAttrs
        (
          config.services.terracomputeOps.enable
          && (
            config.services.terracomputeOps.notifier.enable
            || config.services.terracomputeOps.operatorInput.enable
            || config.services.terracomputeOps.actions.enable
          )
        )
        {
          terracompute-telegram-bot-token.restartUnits =
            lib.optionals config.services.terracomputeOps.notifier.enable [
              "terracompute-notifier.service"
            ]
            ++ lib.optionals config.services.terracomputeOps.operatorInput.enable [
              "terracompute-operator-input.service"
            ]
            ++ lib.optionals config.services.terracomputeOps.actions.enable [
              "terracompute-actions.service"
            ];
        }
    //
      lib.optionalAttrs
        (
          config.services.terracomputeOps.enable
          && (
            config.services.terracomputeOps.notifier.enable
            || config.services.terracomputeOps.operatorInput.enable
          )
        )
        {
          # The action service posts only to its configured group and never reads this.
          terracompute-telegram-chat-id.restartUnits =
            lib.optionals config.services.terracomputeOps.notifier.enable [
              "terracompute-notifier.service"
            ]
            ++ lib.optionals config.services.terracomputeOps.operatorInput.enable [
              "terracompute-operator-input.service"
            ];
        }
    //
      lib.optionalAttrs
        (config.services.terracomputeOps.enable && config.services.terracomputeOps.backup.enable)
        {
          terracompute-backup-restic-password.restartUnits = [ "terracompute-backup.service" ];
          terracompute-backup-ssh-identity.restartUnits = [
            "terracompute-backup-preflight-fetch.service"
            "terracompute-backup.service"
          ];
          terracompute-backup-known-hosts.restartUnits = [
            "terracompute-backup-preflight-fetch.service"
            "terracompute-backup.service"
          ];
        }
    //
      lib.optionalAttrs
        (config.services.terracomputeOps.enable && config.services.terracomputeOps.actions.enable)
        {
          # The restricted key that may only run the target's monitoring-restart helper.
          terracompute-actor-ssh-identity.restartUnits = [ "terracompute-actions.service" ];
        }
    //
      lib.optionalAttrs
        (config.services.terracomputeOps.enable && config.services.terracomputeOps.display.enable)
        {
          # The key the host accepts only for `terra receive`. The display unit is a
          # 30-second oneshot, so its next run picks up a rotated key.
          terracompute-display-ssh-identity = { };
          # The display's own Vast key: machine_read and billing_read, nothing else, so
          # the screen can show reliability and earnings without widening the
          # collector's key.
          terracompute-display-vast-api-key = { };
        }
    //
      lib.optionalAttrs
        (config.services.terracomputeOps.enable && config.services.terracomputeOps.watchdog.enable)
        {
          terracompute-healthchecks-ping-url.restartUnits = [ "terracompute-watchdog.service" ];
        }
    // lib.optionalAttrs config.services.terracomputeL2tp.enable {
      terracompute-l2tp-server.restartUnits = [ "strongswan-swanctl.service" ];
      terracompute-l2tp-ipsec-psk.restartUnits = [ "strongswan-swanctl.service" ];
      terracompute-l2tp-username.restartUnits = [ "strongswan-swanctl.service" ];
      terracompute-l2tp-password.restartUnits = [ "strongswan-swanctl.service" ];
    };
  };

  # Applied outside the sops block so the option is easy to find, matching
  # pelargir/secrets.nix.
  users.users.edgar.hashedPasswordFile = config.sops.secrets.edgar_password_hash.path;

  # With a real console password present, wheel can be required to type it —
  # but only once there IS one. ./default.nix keeps sudo passwordless; revisit
  # that together with this file rather than separately, so the host never ends
  # up with neither route.
}
