# nardol keeps the OS and /srv on one filesystem, so all its free space is one
# pool. The cost: nothing fixed stops Docker, games or models from filling the
# disk the OS runs on, and ext4's reserved blocks don't help because Docker runs
# as root. These two units are what's left in place of that protection.
#
# Disk full? Delete the spare file below for instant room to work, then clear
# what filled the disk. The spare comes back at the next boot once there's room.
{
  lib,
  pkgs,
  ...
}:
let
  spareDir = "/var/lib/nardol-disk-spare";
  spareFile = "${spareDir}/delete-me-if-the-disk-is-full";
  spareSize = "50G";
  # Only recreate the spare with this much free, so it can't cause the problem.
  spareNeedsFreeGiB = 200;
  # Below this the check fails, which shows in `systemctl --failed` and in the
  # Beszel systemd view. A failed unit marks the host degraded, which gaming
  # arbitration accepts.
  warnBelowFreeGiB = 100;
  freeGiB = ''"$(${pkgs.coreutils}/bin/df --output=avail -BG / | ${pkgs.coreutils}/bin/tail -n 1 | ${pkgs.coreutils}/bin/tr -dc 0-9)"'';
in
{
  systemd.services.nardol-disk-spare = {
    description = "Keep a spare file on nardol's disk to delete in an emergency";
    wantedBy = [ "multi-user.target" ];
    after = [ "local-fs.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      StateDirectory = "nardol-disk-spare";
      StateDirectoryMode = "0700";
    };
    script = ''
      test ! -e ${lib.escapeShellArg spareFile} || exit 0
      free=${freeGiB}
      if [ "$free" -lt ${toString spareNeedsFreeGiB} ]; then
        echo "only ''${free}G free: not recreating the spare file" >&2
        exit 0
      fi
      ${pkgs.util-linux}/bin/fallocate -l ${spareSize} ${lib.escapeShellArg spareFile}
    '';
  };

  systemd.services.nardol-disk-free-check = {
    description = "Fail visibly when nardol's disk is nearly full";
    serviceConfig.Type = "oneshot";
    script = ''
      free=${freeGiB}
      if [ "$free" -lt ${toString warnBelowFreeGiB} ]; then
        echo "only ''${free}G free on /. For room to work: rm ${spareFile}" >&2
        exit 1
      fi
    '';
  };

  # This config has no /srv mount, so switching to it on a host that still has
  # the old nardol-fast volume open would unmount /srv under live services and
  # bring them back up on an empty directory. `boot` stays allowed: the data move
  # installs the generation that way, then moves the data before rebooting.
  system.preSwitchChecks.nardolSingleVolume = ''
    if [ "''${2-}" = switch ] || [ "''${2-}" = test ]; then
      if [ -e /dev/mapper/nardol-fast ]; then
        echo "nardol-fast is still open: this generation needs the /srv data move first" >&2
        false
      fi
    fi
  '';

  systemd.timers.nardol-disk-free-check = {
    description = "Check nardol's free disk space every 15 minutes";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "5min";
      OnUnitActiveSec = "15min";
    };
  };
}
