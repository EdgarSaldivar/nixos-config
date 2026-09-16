# L2TP/IPsec transport for the observation-only terracompute supervisor.
#
# The transport has its own commissioning latch so it can be proven before the
# controller starts. Merely importing it must not install a daemon, publish a
# route, or require secrets.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.terracomputeL2tp;
  opsCfg = config.services.terracomputeOps;
  runtimeDirectory = "/run/terracompute-l2tp";
  runtimeConfig = "${runtimeDirectory}/current";
  pppInterface = "ppp-terra";
  guardedHosts = [
    "10.50.0.2/32"
    "10.0.15.237/32"
  ];
  guardMetric = 32760;
  tunnelMetric = 50;

  xl2tpdWrapped = pkgs.stdenv.mkDerivation {
    name = "terracompute-xl2tpd-wrapped";
    nativeBuildInputs = [ pkgs.makeWrapper ];
    buildCommand = ''
      mkdir -p "$out/bin"
      makeWrapper ${pkgs.ppp}/sbin/pppd "$out/bin/pppd" \
        --set LD_PRELOAD "${pkgs.libredirect}/lib/libredirect.so" \
        --set NIX_REDIRECTS "/var/run=/run/pppd"
      makeWrapper ${pkgs.xl2tpd}/bin/xl2tpd "$out/bin/xl2tpd" \
        --set LD_PRELOAD "${pkgs.libredirect}/lib/libredirect.so" \
        --set NIX_REDIRECTS "${pkgs.ppp}/sbin/pppd=$out/bin/pppd"
    '';
  };

  # The hooks contain no credentials.  A more-specific usable route coexists
  # with the high-metric unreachable route while IPCP is up; removing it makes
  # the guard immediately effective again, including after an unclean redial.
  pppUp = pkgs.writeShellApplication {
    name = "terracompute-l2tp-ip-up";
    runtimeInputs = [ pkgs.iproute2 ];
    text = ''
      if_name="$1"
      test "$if_name" = ${lib.escapeShellArg pppInterface}

      ${lib.concatMapStringsSep "\n" (host: ''
        ip -4 route replace ${host} dev ${pppInterface} metric ${toString tunnelMetric}
      '') guardedHosts}
    '';
  };

  pppDown = pkgs.writeShellApplication {
    name = "terracompute-l2tp-ip-down";
    runtimeInputs = [ pkgs.iproute2 ];
    text = ''
      if_name="$1"
      test "$if_name" = ${lib.escapeShellArg pppInterface}

      ${lib.concatMapStringsSep "\n" (host: ''
        ip -4 route del ${host} dev ${pppInterface} metric ${toString tunnelMetric} \
          2>/dev/null || true
      '') guardedHosts}
    '';
  };

  # Convert each byte into pppd's documented octal escape form.  Unlike shell
  # quoting, this safely represents spaces, quotes, backslashes, comment marks,
  # and line breaks without ever putting a credential in argv or the environment.
  prepareRuntimeConfig = pkgs.writeShellApplication {
    name = "terracompute-l2tp-prepare";
    runtimeInputs = [
      pkgs.coreutils
      pkgs.gawk
      pkgs.gnugrep
    ];
    text = ''
            runtime=${lib.escapeShellArg runtimeDirectory}
            credentials="$CREDENTIALS_DIRECTORY"
            umask 0077

            for credential in server ipsec-psk username password; do
              test -f "$credentials/$credential"
              test -s "$credentials/$credential"
              test "$(wc -c < "$credentials/$credential")" -le 4096
            done
            test "$(wc -c < "$credentials/username")" -le 512
            test "$(wc -c < "$credentials/password")" -le 512

            # A server is configuration data delivered as a credential because it
            # participates in the same atomic commissioning/rotation transaction.
            # Keep it to a single DNS name or IPv4 address before interpolating it.
            server="$(head -n 1 "$credentials/server")"
            test -n "$server"
            test "$(printf '%s' "$server" | wc -c)" \
              -eq "$(tr -d '\r\n' < "$credentials/server" | wc -c)"
            printf '%s' "$server" | grep -Eq \
              '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$'
            case "$server" in
              *..* | *.-* | *-.*) exit 1 ;;
            esac

            stage="$(mktemp -d "$runtime/.generation.XXXXXX")"
            trap 'rm -rf "$stage" "$runtime/.current.new"' EXIT

            {
              cat <<EOF
      connections {
        terracompute-l2tp {
          version = 1
          local_addrs = 0.0.0.0
          remote_addrs = $server
          proposals = aes256-sha256-modp2048,aes256-sha1-modp2048,aes256-sha1-modp1024
          keyingtries = 0
          dpd_delay = 30s
          fragmentation = yes

          local {
            auth = psk
          }
          remote {
            auth = psk
          }
          children {
            l2tp-transport {
              mode = transport
              local_ts = dynamic[udp/1701]
              remote_ts = dynamic[udp/1701]
              esp_proposals = aes256-sha256,aes256-sha1
              start_action = start
              close_action = restart
              dpd_action = restart
              rekey_time = 50m
              life_time = 60m
            }
          }
        }
      }
      secrets {
        ike-terracompute {
      EOF
              printf '    secret = 0x'
              od -An -v -t x1 "$credentials/ipsec-psk" | tr -d '[:space:]'
              cat <<'EOF'

        }
      }
      EOF
            } > "$stage/swanctl.conf"

            cat > "$stage/xl2tpd.conf" <<EOF
      [global]
      port = 1701
      access control = yes
      debug avp = no
      debug network = no
      debug packet = no
      debug state = no
      debug tunnel = no

      [lac terracompute]
      lns = $server
      pppoptfile = $runtime/current/ppp-options
      redial = yes
      redial timeout = 10
      require authentication = no
      autodial = no
      EOF

            cat > "$stage/ppp-options" <<'EOF'
      ifname ppp-terra
      ipcp-accept-local
      ipcp-accept-remote
      noipdefault
      nodefaultroute
      nodefaultroute6
      noipv6
      noauth
      noktune
      refuse-eap
      noccp
      nobsdcomp
      nodeflate
      novj
      novjccomp
      mtu 1400
      mru 1400
      persist
      maxfail 0
      holdoff 10
      lcp-echo-interval 30
      lcp-echo-failure 4
      hide-password
      nolog
      noresolvconf
      ip-up-script ${lib.getExe pppUp}
      ip-down-script ${lib.getExe pppDown}
      EOF

            # pppd's option lexer decodes each \ooo sequence back to one byte.  Reject
            # NUL because pppd stores option values as C strings and could truncate it.
            for credential in username password; do
              if od -An -v -t o1 "$credentials/$credential" \
                | awk '{ for (i = 1; i <= NF; i++) if ($i == "000") exit 1 }'; then
                :
              else
                exit 1
              fi
            done
      {
        printf 'name '
        od -An -v -t o1 "$credentials/username" \
          | awk '{ for (i = 1; i <= NF; i++) printf "\\%s", $i } END { print "" }'
        printf 'password '
        od -An -v -t o1 "$credentials/password" \
          | awk '{ for (i = 1; i <= NF; i++) printf "\\%s", $i } END { print "" }'
      } >> "$stage/ppp-options"

            chmod 0600 "$stage/swanctl.conf" "$stage/xl2tpd.conf" "$stage/ppp-options"
            ln -s "$(basename "$stage")" "$runtime/.current.new"
            mv -Tf "$runtime/.current.new" "$runtime/current"
            trap - EXIT
    '';
  };

  runL2tp = pkgs.writeShellApplication {
    name = "terracompute-l2tp-run";
    runtimeInputs = [
      pkgs.coreutils
      pkgs.gnugrep
      pkgs.iproute2
      pkgs.systemd
    ];
    text = ''
      control=${lib.escapeShellArg "${runtimeDirectory}/xl2tpd-control"}
      ${xl2tpdWrapped}/bin/xl2tpd -D \
        -c ${runtimeConfig}/xl2tpd.conf \
        -p ${runtimeDirectory}/xl2tpd.pid \
        -C "$control" &
      daemon_pid=$!
      trap 'kill "$daemon_pid" 2>/dev/null || true; wait "$daemon_pid" 2>/dev/null || true' EXIT

      for _ in $(seq 1 50); do
        if test -p "$control"; then
          printf 'c terracompute\n' > "$control"
          break
        fi
        kill -0 "$daemon_pid"
        sleep 0.2
      done
      test -p "$control"

      ready=false
      for _ in $(seq 1 60); do
        kill -0 "$daemon_pid"
        if ip link show ${pppInterface} >/dev/null 2>&1 \
          ${lib.concatMapStringsSep "" (host: ''
            && ip -4 route show ${host} | grep -q 'dev ${pppInterface}' \
          '') guardedHosts}
        then
          ready=true
          break
        fi
        sleep 1
      done
      "$ready"
      # writeShellApplication's shell is the service main process.  Attribute
      # READY=1 to that parent so NotifyAccess=main accepts the notification.
      systemd-notify --pid=parent --ready --status='Terracompute L2TP routes ready'

      while kill -0 "$daemon_pid" && ip link show ${pppInterface} >/dev/null 2>&1; do
        sleep 5
      done
      exit 1
    '';
  };
in
{
  options.services.terracomputeL2tp.enable = lib.mkEnableOption "the fail-closed Terracompute L2TP/IPsec transport";

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = lib.versions.major pkgs.strongswan.version == "6";
        message = "terracompute L2TP requires strongSwan 6";
      }
      {
        assertion = !config.services.xl2tpd.enable;
        message = "terracompute L2TP uses its own client-mode xl2tpd service";
      }
      {
        assertion = opsCfg.observationOnly;
        message = "terracompute L2TP may only serve the observation-only supervisor";
      }
      {
        assertion = config.networking.firewall.enable;
        message = "terracompute L2TP requires the NixOS firewall";
      }
    ];

    boot.kernelModules = [
      "xfrm_user"
      "ppp_generic"
      "pppox"
      "ppp_async"
      "ppp_mppe"
      "l2tp_core"
      "l2tp_netlink"
      "l2tp_ppp"
    ];

    services.strongswan-swanctl = {
      enable = true;
      package = pkgs.strongswan;
      includes = [ "${runtimeConfig}/swanctl.conf" ];
      strongswan.extraConfig = ''
        charon-systemd {
          journal {
            default = -1
          }
        }
      '';
    };

    systemd.services = {
      strongswan-swanctl = {
        unitConfig = {
          StartLimitIntervalSec = "15min";
          StartLimitBurst = 8;
        };
        serviceConfig = {
          ExecStartPre = lib.getExe prepareRuntimeConfig;
          LoadCredential = [
            "server:${config.sops.secrets.terracompute-l2tp-server.path}"
            "ipsec-psk:${config.sops.secrets.terracompute-l2tp-ipsec-psk.path}"
            "username:${config.sops.secrets.terracompute-l2tp-username.path}"
            "password:${config.sops.secrets.terracompute-l2tp-password.path}"
          ];
          RuntimeDirectory = "terracompute-l2tp";
          RuntimeDirectoryMode = "0700";
          Restart = lib.mkForce "on-failure";
          RestartSec = "5s";
          RestartSteps = 6;
          RestartMaxDelaySec = "5min";
          StandardOutput = "null";
          StandardError = "null";

          UMask = "0077";
          NoNewPrivileges = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          PrivateTmp = true;
          ProtectClock = true;
          ProtectHostname = true;
          ProtectKernelTunables = true;
          ProtectKernelModules = true;
          ProtectKernelLogs = true;
          ProtectControlGroups = true;
          RestrictNamespaces = true;
          RestrictRealtime = true;
          RestrictSUIDSGID = true;
          LockPersonality = true;
          SystemCallArchitectures = "native";
          RestrictAddressFamilies = [
            "AF_UNIX"
            "AF_INET"
            "AF_INET6"
            "AF_NETLINK"
            # charon-systemd probes link-layer interfaces during startup and
            # crashes in strongSwan 6.0.7 when AF_PACKET is denied.
            "AF_PACKET"
          ];
        };
      };

      terracompute-l2tp-route-guards = {
        description = "Install persistent fail-closed terracompute host routes";
        wantedBy = [ "multi-user.target" ];
        before = [ "terracompute-l2tp.service" ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = pkgs.writeShellScript "terracompute-l2tp-route-guards" ''
            set -eu
            ${lib.concatMapStringsSep "\n" (host: ''
              ${lib.getExe' pkgs.iproute2 "ip"} -4 route replace unreachable ${host} metric ${toString guardMetric}
            '') guardedHosts}
          '';
          ExecStop = pkgs.writeShellScript "terracompute-l2tp-route-guards-stop" ''
            set -eu
            ${lib.concatMapStringsSep "\n" (host: ''
              ${lib.getExe' pkgs.iproute2 "ip"} -4 route del unreachable ${host} metric ${toString guardMetric} \
                2>/dev/null || true
            '') guardedHosts}
          '';
          NoNewPrivileges = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          PrivateTmp = true;
          PrivateDevices = true;
          ProtectKernelTunables = true;
          ProtectKernelModules = true;
          ProtectControlGroups = true;
          RestrictNamespaces = true;
          RestrictSUIDSGID = true;
          LockPersonality = true;
          MemoryDenyWriteExecute = true;
          CapabilityBoundingSet = [ "CAP_NET_ADMIN" ];
          AmbientCapabilities = [ "CAP_NET_ADMIN" ];
          RestrictAddressFamilies = [ "AF_NETLINK" ];
        };
      };

      terracompute-l2tp = {
        description = "Terracompute client-mode xl2tpd/pppd";
        wantedBy = [ "multi-user.target" ];
        requires = [
          "strongswan-swanctl.service"
          "terracompute-l2tp-route-guards.service"
        ];
        after = [
          "network-online.target"
          "strongswan-swanctl.service"
          "terracompute-l2tp-route-guards.service"
        ];
        wants = [ "network-online.target" ];
        partOf = [ "strongswan-swanctl.service" ];
        unitConfig = {
          StartLimitIntervalSec = "15min";
          StartLimitBurst = 8;
        };
        serviceConfig = {
          Type = "notify";
          NotifyAccess = "main";
          ExecStart = lib.getExe runL2tp;
          ExecStopPost = "${lib.getExe pppDown} ${pppInterface}";
          Restart = "on-failure";
          RestartSec = "5s";
          RestartSteps = 6;
          RestartMaxDelaySec = "5min";
          # Allow the 10-second xl2tpd control-socket window plus the full
          # 60-second PPP/readiness window before systemd declares failure.
          TimeoutStartSec = "75s";
          TimeoutStopSec = "30s";
          KillMode = "control-group";
          UMask = "0077";
          StandardOutput = "null";
          StandardError = "null";

          User = "root";
          Group = "root";
          NoNewPrivileges = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          PrivateTmp = true;
          PrivateDevices = false;
          ProtectClock = true;
          ProtectHostname = true;
          ProtectKernelTunables = true;
          ProtectKernelModules = true;
          ProtectKernelLogs = true;
          ProtectControlGroups = true;
          ProtectProc = "invisible";
          ProcSubset = "pid";
          RestrictNamespaces = true;
          RestrictRealtime = true;
          RestrictSUIDSGID = true;
          LockPersonality = true;
          MemoryDenyWriteExecute = true;
          CapabilityBoundingSet = [
            "CAP_NET_ADMIN"
            "CAP_NET_RAW"
            "CAP_SYS_TTY_CONFIG"
          ];
          AmbientCapabilities = [
            "CAP_NET_ADMIN"
            "CAP_NET_RAW"
            "CAP_SYS_TTY_CONFIG"
          ];
          RuntimeDirectory = "pppd";
          RuntimeDirectoryPreserve = true;
          # strongswan-swanctl owns this runtime directory, while xl2tpd must
          # create its control FIFO and PID file inside it.
          ReadWritePaths = [ runtimeDirectory ];
          RestrictAddressFamilies = [
            "AF_UNIX"
            "AF_INET"
            "AF_NETLINK"
            "AF_PPPOX"
          ];
        };
      };
      terracompute-collector = lib.mkIf (opsCfg.enable && opsCfg.collector.enable) {
        requires = [ "terracompute-l2tp.service" ];
        after = [ "terracompute-l2tp.service" ];
      };
    };

    networking.firewall = {
      extraCommands = ''
        iptables -w -I nixos-fw 1 \
          -p udp --sport 1701 --dport 1701 \
          -j REJECT --reject-with icmp-port-unreachable
        iptables -w -I nixos-fw 1 \
          -p udp --sport 1701 --dport 1701 \
          -m policy --dir in --pol ipsec --mode transport \
          -m comment --comment "terracompute protected L2TP input" \
          -j nixos-fw-accept

        iptables -w -N terracompute-l2tp-output 2>/dev/null || true
        iptables -w -F terracompute-l2tp-output
        iptables -w -A terracompute-l2tp-output \
          -p udp --sport 1701 --dport 1701 \
          -m policy --dir out --pol ipsec --mode transport \
          -j ACCEPT
        iptables -w -A terracompute-l2tp-output \
          -p udp --sport 1701 --dport 1701 \
          -j REJECT --reject-with icmp-port-unreachable
        while iptables -w -D OUTPUT -j terracompute-l2tp-output 2>/dev/null; do :; done
        iptables -w -I OUTPUT 1 -j terracompute-l2tp-output

        iptables -w -N terracompute-ppp-output 2>/dev/null || true
        iptables -w -F terracompute-ppp-output
        iptables -w -A terracompute-ppp-output -d 10.50.0.2/32 -j ACCEPT
        iptables -w -A terracompute-ppp-output -d 10.0.15.237/32 -j ACCEPT
        iptables -w -A terracompute-ppp-output \
          -j REJECT --reject-with icmp-admin-prohibited
        while iptables -w -D OUTPUT -o ${pppInterface} -j terracompute-ppp-output 2>/dev/null; do :; done
        iptables -w -I OUTPUT 1 -o ${pppInterface} -j terracompute-ppp-output
      '';
      extraStopCommands = ''
        iptables -w -D nixos-fw \
          -p udp --sport 1701 --dport 1701 \
          -m policy --dir in --pol ipsec --mode transport \
          -m comment --comment "terracompute protected L2TP input" \
          -j nixos-fw-accept 2>/dev/null || true
        iptables -w -D nixos-fw \
          -p udp --sport 1701 --dport 1701 \
          -j REJECT --reject-with icmp-port-unreachable 2>/dev/null || true
        iptables -w -D OUTPUT -o ${pppInterface} \
          -j terracompute-ppp-output 2>/dev/null || true
        iptables -w -F terracompute-ppp-output 2>/dev/null || true
        iptables -w -X terracompute-ppp-output 2>/dev/null || true
        iptables -w -D OUTPUT -j terracompute-l2tp-output 2>/dev/null || true
        iptables -w -F terracompute-l2tp-output 2>/dev/null || true
        iptables -w -X terracompute-l2tp-output 2>/dev/null || true
      '';
    };
  };
}
