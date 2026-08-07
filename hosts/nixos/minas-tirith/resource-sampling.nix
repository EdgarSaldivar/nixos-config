# minas-tirith — sustained per-container resource sampling for A.5.
#
# WHY THIS EXISTS
# ---------------
# The migration ledger gates every service on a measured resource request: "a row
# without one does not migrate". The only data we had was a SINGLE `docker stats`
# reading, and that turned out to be actively misleading in two separate ways:
#
#   1. `docker stats` reports `memory.current`, which INCLUDES page cache. Measured
#      against the cgroups, plex reported 2060M and was really 353M; sonarr reported
#      971M and was really 183M; deluge-vpn reported ~25 GB of which 29.7 GB was page
#      cache and only 194 MB was anonymous. Sizing requests from that column would
#      have over-reserved several-fold across the fleet.
#   2. A single sample is taken at one arbitrary moment. An idle sample sets a request
#      that is too small exactly when load arrives — a Plex transcode, an *arr import
#      run — which is when being wrong actually costs something.
#
# So this records the RIGHT numbers, REPEATEDLY, and starts accumulating now rather
# than the day someone sits down to write 35 manifests.
#
# WHAT IS RECORDED, AND WHY EACH COLUMN
# -------------------------------------
#   anon        memory that CANNOT be reclaimed. This is what a memory REQUEST should
#               be sized from -- it is the only part the scheduler must truly reserve.
#   file        page cache. Reclaimable, and the reason `docker stats` misleads.
#   workingset  memory.current - inactive_file: what KUBELET measures and evicts on.
#               Note this is NOT "anon" -- if page cache is active it counts here,
#               which is why deluge-vpn shows a ~30 GB working set while being a
#               200 MB process. Sizing a memory LIMIT is what bounds this.
#   cpu_usec    CUMULATIVE cpu time. Deliberately cumulative rather than a
#               percentage: rates are derived downstream from two samples, so a
#               missed or delayed run cannot corrupt the series the way a
#               self-computed instantaneous percentage would.
#
# SCOPE: docker only, on purpose. This measures the MIGRATION SOURCE. Once a service
# is running under k3s, metrics-server and the cluster's own metrics cover it, and
# what matters then is whether the request we chose was right -- a different question
# from this one.
{ config, pkgs, ... }:
let
  sampler = pkgs.writeShellScript "sample-container-resources" ''
    set -u
    out=/var/lib/resource-samples/samples.csv
    install -d -m 0755 /var/lib/resource-samples

    # Header only once, so the file can be appended to forever and still be a valid CSV.
    if [ ! -s "$out" ]; then
      echo "ts,container,anon,file,workingset,cpu_usec" > "$out"
    fi

    # Guard against unbounded growth. At ~32 containers every 5 minutes this is on the
    # order of 500 KB/day, so 200 MB is many months -- but a file that grows without
    # any bound is how a disk fills at 3am.
    if [ -f "$out" ] && [ "$(stat -c %s "$out")" -gt 209715200 ]; then
      mv "$out" "$out.1"
      echo "ts,container,anon,file,workingset,cpu_usec" > "$out"
    fi

    ts=$(date +%s)
    for id in $(docker ps -q 2>/dev/null); do
      name=$(docker inspect -f '{{.Name}}' "$id" 2>/dev/null | sed 's|^/||')
      [ -z "$name" ] && continue

      # cgroup v2 with the systemd driver puts container cgroups here. The `find`
      # fallback exists because the scope path differs under other cgroup drivers,
      # and a sampler that silently records nothing is worse than one that is absent.
      cg=/sys/fs/cgroup/system.slice/docker-$id.scope
      if [ ! -f "$cg/memory.stat" ]; then
        cg=$(find /sys/fs/cgroup -maxdepth 4 -type d -name "*$id*" 2>/dev/null | head -1)
      fi
      [ -z "$cg" ] || [ ! -f "$cg/memory.stat" ] && continue

      cur=$(cat "$cg/memory.current" 2>/dev/null || echo 0)
      anon=$(awk '/^anon /{print $2; exit}' "$cg/memory.stat" 2>/dev/null || echo 0)
      file=$(awk '/^file /{print $2; exit}' "$cg/memory.stat" 2>/dev/null || echo 0)
      inact=$(awk '/^inactive_file /{print $2; exit}' "$cg/memory.stat" 2>/dev/null || echo 0)
      cpu=$(awk '/^usage_usec /{print $2; exit}' "$cg/cpu.stat" 2>/dev/null || echo 0)
      ws=$(( cur - inact ))

      echo "$ts,$name,$anon,$file,$ws,$cpu" >> "$out"
    done
  '';
in
{
  systemd.services.resource-sampling = {
    description = "Sample per-container cgroup memory and CPU for A.5 request sizing";
    # systemd.services.path is the ONLY PATH this gets -- nothing is inherited. An
    # earlier script in this repo shipped without gawk and failed with
    # `awk: command not found` while still exiting 0, so the check silently did
    # nothing while reporting success. Every binary used above is listed here.
    path = with pkgs; [
      docker
      coreutils
      gawk
      gnused
      findutils
    ];
    serviceConfig = {
      Type = "oneshot";
      # Sampling must never be the thing that wedges the box, and a sampler that
      # overruns its own interval is a bug, not a slow disk.
      TimeoutStartSec = "120s";
      ExecStart = sampler;
    };
  };

  systemd.timers.resource-sampling = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "5m";
      OnUnitActiveSec = "5m";
      # No RandomizedDelaySec: an even cadence makes the CPU rate derived between two
      # consecutive samples meaningful. Jitter would be fine for load-spreading and
      # is actively unhelpful for a time series.
      AccuracySec = "10s";
    };
  };
}
