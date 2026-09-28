# pelargir — the target directory of PinCollector's nightly backup.
#
# The backup itself is containerized: CronJob `backup` in the pin-collector namespace
# (minas-tirith/manifests/pin-collector.yaml.in) dumps PostgreSQL, mirrors Garage with the
# read-only backup key, checks every object the dump references, and keeps a restic
# repository of both. It runs on pelargir and reaches this directory through a static local
# PersistentVolume (the namespace is Restricted, so no hostPath).
#
# All the host does is make the directory usable:
#   - a real directory on pelargir's root filesystem, owned by the pods' user
#     (10001:10001), group-writable and setgid, which is what kubelet's OnRootMismatch
#     accepts without walking the tree. Its subdirectories (work/, mirror/, restic/) are
#     created by the Job, not here;
#   - the sentinel every container of the Job requires before it writes anything: the
#     directory's device:inode, refreshed on every run.
#
# Not inside pelargir's own restic backup (backup.nix), whose paths are
# /var/lib/restic-staging/pelargir plus copies of /var/lib/rancher/k3s/storage: this
# directory is neither, so the PinCollector repository is not copied again to minas.
{ pkgs, ... }:
let
  target = "/var/lib/pincollector-backup";
  # Root-owned and not writable by the pod's user: the sentinel is built here.
  staging = "/var/lib";
  uid = "10001";
in
{
  systemd.services.pincollector-backup-target = {
    description = "Prepare the PinCollector backup target for its CronJob";
    wantedBy = [ "multi-user.target" ];
    before = [ "k3s.service" ];
    unitConfig.RequiresMountsFor = [ target ];
    path = [
      pkgs.coreutils
      pkgs.gnugrep
      pkgs.util-linux
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      set -eu
      # mkdir, not install -d: install resets an existing directory's owner to root.
      mkdir -p ${target}
      # Everything below mutates ${target}: it must be a real directory on the root
      # filesystem, not a symlink elsewhere or a separate filesystem mounted there.
      if [ -L ${target} ] || [ ! -d ${target} ]; then
        echo "${target} is not a real directory" >&2
        exit 1
      fi
      [ "$(findmnt -n -o TARGET --target ${target})" = / ] || exit 1
      # Enumerate first, outside any condition, so a findmnt failure stops the unit (set -e)
      # instead of reading as "no nested mounts".
      mounts=$(findmnt -n -r -o TARGET)
      case "$(printf '%s\n' "$mounts" | grep -c '^${target}\(/\|$\)' || true)" in
        0) ;;
        *) echo "a filesystem is mounted at or inside ${target}" >&2; exit 1 ;;
      esac
      # The Job's fsGroup is ${uid}. A root already group-owned, group-writable and
      # setgid is what kubelet's OnRootMismatch accepts, so it never walks the repository.
      chown -h ${uid}:${uid} ${target}
      chmod 2770 ${target}
      # The sentinel names this directory by device:inode, rewritten on every run. Each stage
      # compares it with `stat -c %d:%i /backup` (a bind mount keeps both), so a sentinel
      # that outlived its directory, or a PV pointed at some other one, is refused.
      sentinel=${target}/.pincollector-backup-target
      identity=$(stat -c %d:%i ${target})
      # ${target} is writable by the pod's user, so never write through a path there:
      # build the file in ${staging}, which that user cannot write, and rename it in.
      # rename(2) replaces the entry itself and never follows it, -T refuses to move onto a
      # directory, and both paths are on the root filesystem, so the rename is atomic.
      tmp=$(mktemp ${staging}/.pincollector-backup-sentinel.XXXXXX)
      printf '%s\n' "$identity" > "$tmp"
      chmod 0444 "$tmp"
      chown ${uid}:${uid} "$tmp"
      mv -T -f "$tmp" "$sentinel"
    '';
  };
}
