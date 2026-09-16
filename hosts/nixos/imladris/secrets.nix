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
      # commissioning step feeds to `smbpasswd -s`.
      samba_password = { };

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
    // lib.optionalAttrs config.services.terracomputeOps.enable {
      # These encrypted values are installed only after the explicit global
      # commissioning latch is enabled. Disabled evaluation still validates the
      # nonsecret role configuration without materializing unused credentials.
      terracompute-ssh-identity.restartUnits = [ "terracompute-collector.service" ];
      terracompute-known-hosts.restartUnits = [ "terracompute-collector.service" ];
      terracompute-vast-read-api-key.restartUnits = [ "terracompute-collector.service" ];
      terracompute-bmc-password.restartUnits = [ "terracompute-collector.service" ];
      terracompute-telegram-bot-token.restartUnits = [
        "terracompute-notifier.service"
        "terracompute-operator-input.service"
      ];
      terracompute-telegram-chat-id.restartUnits = [
        "terracompute-notifier.service"
        "terracompute-operator-input.service"
      ];
      terracompute-backup-restic-password.restartUnits = [ "terracompute-backup.service" ];
      terracompute-backup-ssh-identity.restartUnits = [
        "terracompute-backup-preflight-fetch.service"
        "terracompute-backup.service"
      ];
      terracompute-backup-known-hosts.restartUnits = [
        "terracompute-backup-preflight-fetch.service"
        "terracompute-backup.service"
      ];
      terracompute-healthchecks-ping-url.restartUnits = [ "terracompute-watchdog.service" ];
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
