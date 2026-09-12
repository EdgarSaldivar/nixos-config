{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:

# A password hash that sops decrypts too late leaves the account LOCKED, and
# says nothing about it.
#
# `users.users.<name>.hashedPasswordFile` is read by the `users` activation
# script. Plain `sops.secrets.<name> = { }` decrypts into /run/secrets during a
# LATER activation step, so on the first activation the file does not exist yet.
# The users script then emits
#
#   warning: password file '/run/secrets/edgar_password_hash' does not exist
#
# on one line buried in bootloader output, writes `!` into /etc/shadow, and the
# deploy exits 0. sops reports the secret installed, `systemctl` is green, and
# the console login the secret exists to provide silently does not work.
#
# imladris shipped exactly that on 2026-09-11, on the first activation after its
# ./secrets.nix import landed — while osgiliath, minas-tirith and pelargir had
# all been setting neededForUsers since their own commissioning. The fleet knew;
# the newest host did not inherit it.
#
# `neededForUsers = true` moves the secret to /run/secrets-for-users, which sops
# populates BEFORE user creation. That is the only correct place for a password
# hash, so the rule is simply: every hashedPasswordFile must live there.
#
# Checked by path rather than by chasing sops option names because the path is
# the thing the activation script actually opens — it stays true regardless of
# how the secret is declared, or whether it comes from sops at all.
let
  secretsForUsers = "/run/secrets-for-users/";

  offenders = lib.concatMap (
    host:
    let
      cfg = nixosConfigurations.${host}.config;
      bad = lib.filterAttrs (
        _: u:
        (u.hashedPasswordFile or null) != null && !(lib.hasPrefix secretsForUsers u.hashedPasswordFile)
      ) cfg.users.users;
    in
    map (user: "${host}:${user} -> ${cfg.users.users.${user}.hashedPasswordFile}") (lib.attrNames bad)
  ) (lib.attrNames nixosConfigurations);

  # A tautology guard. If every host stopped using hashedPasswordFile the check
  # above would pass vacuously while enforcing nothing, so require that the
  # fleet's console-password posture still exists at all.
  usingPasswordFiles = lib.concatMap (
    host:
    let
      cfg = nixosConfigurations.${host}.config;
    in
    lib.attrNames (lib.filterAttrs (_: u: (u.hashedPasswordFile or null) != null) cfg.users.users)
  ) (lib.attrNames nixosConfigurations);
in
if offenders != [ ] then
  throw ''
    hashedPasswordFile must be decrypted before user creation, or the account is
    left locked while the deploy exits 0. Use `neededForUsers = true` so the
    secret lands under ${secretsForUsers}.
    Offending: ${lib.concatStringsSep ", " offenders}
  ''
else if usingPasswordFiles == [ ] then
  throw "no host sets hashedPasswordFile any more; this check has become a tautology and the console-password posture needs re-examining"
else
  pkgs.runCommand "user-password-file-ordering-ok" { } "touch $out"
