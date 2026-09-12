# imladris — sops-nix wiring. Secret values exist only in /run, never the store.
#
# ⛔ NOT IMPORTED YET. ./default.nix carries this file as a commented import, and
# that is deliberate rather than an oversight.
#
# sops-nix derives this host's age identity from /etc/ssh/ssh_host_ed25519_key,
# so `secrets/imladris.yaml` cannot be encrypted to a recipient that does not
# exist until the machine has a host key. Referencing a sops file that is not on
# disk fails Nix path resolution at EVALUATION time, which would break
# `nix flake check` for the whole repository and on every other host — not just
# here.
#
# Commissioning step 4 of docs/runbooks/imladris/install.md does, in order:
#   1. generate the host key on dol-amroth and stage it for the install
#   2. derive its age recipient: ssh-to-age -i ssh_host_ed25519_key.pub
#   3. add that recipient and a creation_rule for secrets/imladris.yaml to
#      .sops.yaml
#   4. create secrets/imladris.yaml
#   5. uncomment the ./secrets.nix import in ./default.nix
#
# Until step 5 lands, edgar has key-only SSH and passwordless sudo but NO console
# password. That is one way in, not two, which is exactly the state that made
# pelargir unadministrable on 2026-08-04.
{ config, ... }:
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
      edgar_password_hash = { };

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
