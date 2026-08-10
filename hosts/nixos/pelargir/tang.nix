# pelargir — network-bound disk-encryption service for nardol.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  tangPort = 7654;
  nardolAddress = "10.0.0.118";
  tangUrl = "http://127.0.0.1:${toString tangPort}";

  stageTangState = pkgs.writeShellScript "pelargir-stage-tang-state" ''
    set -eu
    source=/var/lib/tang
    staging=/var/lib/restic-staging/pelargir/tang

    if [ ! -d "$source" ]; then
      echo "CRITICAL: Tang state directory $source does not exist" >&2
      exit 1
    fi
    # DynamicUser StateDirectory may expose /var/lib/tang as a symlink into
    # /var/lib/private; -L is therefore required to inspect the actual keys.
    if ! ${pkgs.findutils}/bin/find -L "$source" -mindepth 1 -maxdepth 1 -type f -print -quit \
      | ${pkgs.gnugrep}/bin/grep -q .; then
      echo "CRITICAL: Tang state directory $source contains no keys" >&2
      exit 1
    fi

    ${pkgs.coreutils}/bin/install -d -m 0700 "$staging"
    # Include hidden/retired rotation keys: existing Clevis tokens can continue
    # to depend on them after Tang begins advertising a newer key.
    ${pkgs.rsync}/bin/rsync -a --delete "$source/" "$staging/"

    if ! ${pkgs.findutils}/bin/find -L "$staging" -mindepth 1 -maxdepth 1 -type f -print -quit \
      | ${pkgs.gnugrep}/bin/grep -q .; then
      echo "CRITICAL: staged Tang backup is empty" >&2
      exit 1
    fi
  '';
in
{
  # Tang stores no copy of nardol's passphrase. It performs the server half of
  # a cryptographic exchange using keys in /var/lib/tang. The LUKS passphrase
  # remains an independent recovery slot on nardol.
  services.tang = {
    enable = true;
    listenStream = [ "0.0.0.0:${toString tangPort}" ];
    ipAddressAllow = [
      "127.0.0.1/32"
      "${nardolAddress}/32"
    ];
  };

  # nixpkgs' Accept=yes tangd template gives every short-lived connection the
  # same RuntimeDirectory=tang. Without preservation, one completed instance
  # removes /run/tang while another is entering its mount namespace; concurrent
  # unlock attempts then fail with status 226/NAMESPACE. Keep the otherwise
  # upstream service intact, but retain the shared runtime directory until the
  # next boot. Reproduced on Pelargir with 35 failures from 60 requests before
  # this override.
  systemd.services."tangd@".serviceConfig.RuntimeDirectoryPreserve = true;

  # The socket-level systemd ACL above is the second layer. This nftables rule
  # is deliberately source- and interface-scoped: Tang is not exposed on the
  # tailnet, WireGuard, WAN-facing forwarded traffic, or any k3s interface.
  networking.firewall.extraInputRules = ''
    iifname "eth0" ip saddr ${nardolAddress} tcp dport ${toString tangPort} accept comment "Tang for nardol"
  '';

  # Socket activation creates/serves the advertisement and generates initial
  # state. A timer makes failure visible before a reboot needs Tang.
  systemd.services.pelargir-tang-health = {
    description = "Verify Pelargir Tang advertisement";
    requires = [ "tangd.socket" ];
    after = [ "tangd.socket" ];
    serviceConfig.Type = "oneshot";
    script = ''
      ${pkgs.curl}/bin/curl --fail --silent --show-error --max-time 5 \
        ${tangUrl}/adv >/dev/null
    '';
  };
  systemd.timers.pelargir-tang-health = {
    description = "Check Tang every five minutes";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "1min";
      OnUnitActiveSec = "5min";
      AccuracySec = "30s";
    };
  };

  # The existing restic job backs up only this staging tree. Make staging a
  # required, ordered dependency without editing backup.nix, minimizing overlap
  # with concurrent Pelargir work. A missing/empty Tang key set fails the backup
  # instead of silently publishing an incomplete recovery set.
  systemd.services.pelargir-stage-tang-state = {
    description = "Stage Tang keys for Pelargir restic backup";
    requires = [ "tangd.socket" ];
    after = [ "tangd.socket" ];
    before = [ "restic-backups-minas.service" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = stageTangState;
    };
  };
  systemd.services.restic-backups-minas = {
    requires = [ "pelargir-stage-tang-state.service" ];
    after = [ "pelargir-stage-tang-state.service" ];
  };

  assertions = [
    {
      assertion = config.networking.nftables.enable && config.networking.firewall.backend == "nftables";
      message = "pelargir: the source-scoped Tang rule requires the nftables firewall backend.";
    }
    {
      assertion = lib.elem "${nardolAddress}/32" config.services.tang.ipAddressAllow;
      message = "pelargir: Tang must permit nardol's fixed initrd address.";
    }
  ];
}
