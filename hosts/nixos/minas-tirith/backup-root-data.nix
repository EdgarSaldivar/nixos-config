# minas-tirith — the nightly backup program, its timer and its failure protocol.
#
# ⛔ These three move and stay together. The service, `systemd.timers.backup-root-data`,
# the `notify-failure@` template and the `onFailure` edge implement ONE failure-state
# protocol; separating them would let a future edit change the alerting path without
# touching the thing that alerts.
#
# Split out of system.nix on 2026-08-16. The embedded program is unchanged, byte for
# byte — see hosts/nixos/minas-tirith/scripts/tests/ for the characterization fixtures
# that pin its behaviour.
{ config, pkgs, ... }:
{
  systemd.services.backup-root-data = {
    description = "Back up mutable service data from root to ZFS storage2";
    serviceConfig = {
      Type = "oneshot";
      Nice = 10;
      IOSchedulingClass = "idle";
      # systemd DISABLES the start timeout for Type=oneshot by default. Without
      # this, a wedged rsync or a hung ZFS call sits in `activating` forever,
      # OnFailure never fires, no marker is written — and because the previous
      # run's stamp is still recent, the heartbeat reports healthy for up to 48
      # hours while no backup is actually running. A hang has to become a
      # failure for any of the failure plumbing to mean anything.
      # 6h is generous: the full ~429 GB run measured well under an hour.
      TimeoutStartSec = "6h";
    };
    # ⛔ COMMAND RESOLUTION LIVES HERE, NOT IN THE PROGRAM.
    #
    # Until 2026-08-16 every command in this program was an interpolated store
    # path, which is why the program had to live inside a Nix string. It is now a
    # real `.sh` with bare command names, and this list is what resolves them.
    #
    # The list is EXACTLY the set of packages whose store paths were previously
    # interpolated -- age, coreutils, docker, gnugrep, gnused, gzip, k3s, rsync,
    # sqlite, util-linux, zfs -- and `checks/minas-command-resolution.nix` asserts
    # that each of the 17 commands still resolves to the same store path it was
    # hard-coded to before. Adding a package here can silently shadow a command;
    # that check is what makes it loud.
    path = with pkgs; [
      age
      coreutils
      docker
      gnugrep
      gnused
      gzip
      k3s
      rsync
      sqlite
      util-linux
      zfs
    ];

    # ⛔ NOT `writeShellApplication`, and that is deliberate.
    #
    # ROADMAP item 2 named it, but it exports `runtimeInputs` onto PATH from INSIDE
    # the script. The 22 characterization fixtures work by putting fakes on PATH and
    # running the rendered program against them; a PATH export inside the program
    # puts the real `zfs`, `docker` and `k3s` ahead of those fakes and the entire
    # suite stops testing anything. Keeping resolution in `path` above leaves PATH
    # under the caller's control, which is what makes the fixtures possible at all.
    #
    # `readFile` also keeps `systemd.services.backup-root-data.script` a plain
    # string, so the fixtures and the unit-contract check still evaluate on
    # aarch64-darwin without building an x86_64-linux derivation.
    script = builtins.readFile ./scripts/backup-root-data.sh;
  };
  systemd.timers.backup-root-data = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "daily";
      Persistent = true;
      RandomizedDelaySec = "30m";
    };
  };

  # A failing backup timer is invisible by default — systemd marks the unit
  # failed and nothing else happens.
  #
  # Pinging /fail here is NOT sufficient on its own: the 5-minute heartbeat pings
  # the same check, and a success ping CLEARS a previous failure. So a backup that
  # failed at 03:00 was quietly marked healthy again by 03:05, and a backup that
  # had never succeeded at all reported green indefinitely. The durable marker is
  # what actually holds the alert open — the heartbeat reads it every run and keeps
  # reporting UNHEALTHY until a backup succeeds and removes it.
  systemd.services."notify-failure@" = {
    description = "Report failure of %i to healthchecks";
    serviceConfig = {
      Type = "oneshot";
      # %i is expanded by systemd in unit-file DIRECTIVES only. NixOS's `script`
      # becomes a separate shell file, where a bare %i would stay literal — so
      # the instance name is handed in through Environment=, which does expand.
      Environment = "FAILED_UNIT=%i";
    };
    script = ''
      # head -1, NOT cat: the secret may carry a second line (the dedicated
      # critical check). `cat` would hand curl a URL containing a newline, which
      # it rejects outright — silently losing the immediate failure notification.
      URL="$(head -1 ${config.sops.secrets.healthchecks-url.path})"
      if [ "$FAILED_UNIT" = "backup-root-data" ]; then
        printf '%s failed at %s\n' "$FAILED_UNIT" "$(date -u -Is)" \
          > /var/lib/backup-root-data.failed
      fi
      ${pkgs.curl}/bin/curl -fsS -m 20 \
        --data-raw "UNHEALTHY: systemd unit $FAILED_UNIT FAILED" "$URL/fail" || true
    '';
  };
  systemd.services.backup-root-data.onFailure = [ "notify-failure@backup-root-data.service" ];

}
