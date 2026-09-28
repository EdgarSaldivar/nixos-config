# minas-tirith — the PinCollector training archive, prepared for its pull.
#
# The pull itself is containerized: a hand-started Job in the pin-collector namespace
# (manifests/pin-collector.yaml.in, CronJob `training-pull`) running
# `python -m app.maintenance.training_pull` in the API image. It reaches
# /storage2/pincollector/training/feedback through a static local PersistentVolume.
#
# All the host does is make that directory usable, and only on the real dataset:
#   - hand it once to the image's user (10001:10001), recorded by a marker written only
#     after the whole recursive chown succeeded;
#   - write the sentinel the Job requires. It lives on the ZFS dataset itself, so when the
#     dataset is not mounted, the path the PersistentVolume points at has no sentinel and
#     the pull refuses instead of writing to the pool below.
# The research archive beside it (research-2026-09/) is left alone.
{ pkgs, ... }:
let
  dataset = "storage2/pincollector/training";
  datasetMount = "/storage2/pincollector/training";
  feedback = "${datasetMount}/feedback";
  uid = "10001";
in
{
  systemd.services.pincollector-training-archive = {
    description = "Prepare the PinCollector training archive for its pull Job";
    wantedBy = [ "multi-user.target" ];
    before = [ "k3s.service" ];
    unitConfig.RequiresMountsFor = [ datasetMount ];
    path = [
      pkgs.coreutils
      pkgs.util-linux
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      set -eu
      [ "$(findmnt -n -o SOURCE --target ${datasetMount})" = ${dataset} ] || exit 1
      # mkdir, not install -d: install resets an existing directory's owner to root.
      mkdir -p ${feedback}
      # Everything below mutates ${feedback}: it must be a real directory on the dataset
      # itself, not a symlink elsewhere or a separate filesystem mounted there.
      if [ -L ${feedback} ] || [ ! -d ${feedback} ]; then
        echo "${feedback} is not a real directory" >&2
        exit 1
      fi
      [ "$(findmnt -n -o TARGET --target ${feedback})" = ${datasetMount} ] || exit 1
      # Enumerate first, outside any condition, so a findmnt failure stops the unit (set -e)
      # instead of reading as "no nested mounts".
      mounts=$(findmnt -n -r -o TARGET)
      case "$(printf '%s\n' "$mounts" | grep -c '^${feedback}\(/\|$\)' || true)" in
        0) ;;
        *) echo "a filesystem is mounted at or inside ${feedback}" >&2; exit 1 ;;
      esac
      marker=${datasetMount}/.feedback-owner-migrated-${uid}
      if [ ! -e "$marker" ]; then
        chown -R ${uid}:${uid} ${feedback}
        touch "$marker"
      fi
      # The Job's fsGroup is ${uid}. A root already group-owned, group-writable and
      # setgid is what kubelet's OnRootMismatch accepts, so it never walks the archive.
      chown -h ${uid}:${uid} ${feedback}
      chmod 2775 ${feedback}
      sentinel=${feedback}/.pincollector-training-dataset
      if [ ! -f "$sentinel" ] || [ -L "$sentinel" ]; then
        # ${feedback} is writable by the pod's user, so never write through a path there:
        # build the file in the dataset root, which that user cannot write, and rename it in. rename(2) replaces
        # the entry itself and never follows it, and -T refuses to move into a directory.
        tmp=$(mktemp ${datasetMount}/.sentinel.XXXXXX)
        chmod 0444 "$tmp"
        chown ${uid}:${uid} "$tmp"
        mv -T -f "$tmp" "$sentinel"
      fi
    '';
  };
}
