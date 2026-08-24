{
  lib,
  pkgs,
}:
let
  vban = pkgs.callPackage ../vban.nix { };
  vbanClientAddress = "10.0.0.17";
  vbanPort = 6980;
  vbanStreamName = "Talkie";
  wolfPulseSocket = "/run/wolf/pulse-socket";
  wolfMicSink = "nardol_client_mic_sink";
  wolfMicSource = "nardol_client_mic";
  vbanMicPrepare = pkgs.writeShellApplication {
    name = "nardol-vban-mic-prepare";
    runtimeInputs = [
      pkgs.coreutils
      pkgs.gnugrep
      pkgs.pulseaudio
    ];
    text = ''
      export PULSE_SERVER=unix:${wolfPulseSocket}

      ready=
      for _ in $(seq 1 60); do
        if test -S ${wolfPulseSocket} && pactl info >/dev/null 2>&1; then
          ready=1
          break
        fi
        sleep 1
      done
      test -n "$ready"

      if ! pactl list short sinks | cut -f2 | grep -Fxq ${wolfMicSink}; then
        pactl load-module module-null-sink \
          sink_name=${wolfMicSink} \
          rate=48000 \
          channels=1 \
          channel_map=mono \
          sink_properties=device.description=Nardol-Client-Microphone-Receiver \
          >/dev/null
      fi

      if ! pactl list short sources | cut -f2 | grep -Fxq ${wolfMicSource}; then
        pactl load-module module-remap-source \
          master=${wolfMicSink}.monitor \
          source_name=${wolfMicSource} \
          channels=1 \
          channel_map=mono \
          source_properties=device.description=Nardol-Client-Microphone \
          >/dev/null
      fi
    '';
  };
  vbanMicCleanup = pkgs.writeShellApplication {
    name = "nardol-vban-mic-cleanup";
    runtimeInputs = [
      pkgs.gawk
      pkgs.pulseaudio
    ];
    text = ''
      export PULSE_SERVER=unix:${wolfPulseSocket}
      if ! test -S ${wolfPulseSocket} || ! pactl info >/dev/null 2>&1; then
        exit 0
      fi

      remap_id="$(pactl list short modules | awk \
        '$2 == "module-remap-source" && $0 ~ /source_name=${wolfMicSource}([[:space:]]|$)/ { print $1; exit }')"
      if test -n "$remap_id"; then
        pactl unload-module "$remap_id" || true
      fi

      sink_id="$(pactl list short modules | awk \
        '$2 == "module-null-sink" && $0 ~ /sink_name=${wolfMicSink}([[:space:]]|$)/ { print $1; exit }')"
      if test -n "$sink_id"; then
        pactl unload-module "$sink_id" || true
      fi
    '';
  };
in
{
  environment.etc."nardol/wolf-client-mic.sh" = {
    text = ''
      # Sourced by the Games on Whales entrypoint before it starts Steam.
      export PULSE_SOURCE=${wolfMicSource}
    '';
    mode = "0444";
  };

  systemd.services.nardol-vban-microphone = {
    description = "Receive the Mac VBAN microphone into Wolf PulseAudio";
    wantedBy = [ "multi-user.target" ];
    requires = [ "docker-wolf.service" ];
    after = [ "docker-wolf.service" ];
    partOf = [ "docker-wolf.service" ];
    preStart = lib.getExe vbanMicPrepare;
    script = ''
      export PULSE_SERVER=unix:${wolfPulseSocket}
      exec ${lib.getExe' vban "vban_receptor"} \
        --ipaddress=${vbanClientAddress} \
        --port=${toString vbanPort} \
        --streamname=${vbanStreamName} \
        --backend=pulseaudio \
        --device=${wolfMicSink} \
        --quality=1
    '';
    postStop = lib.getExe vbanMicCleanup;
    serviceConfig = {
      Restart = "on-failure";
      RestartSec = "2s";
      DynamicUser = true;
      UMask = "0077";
      NoNewPrivileges = true;
      PrivateDevices = true;
      PrivateTmp = true;
      ProtectHome = true;
      ProtectSystem = "strict";
      ProtectControlGroups = true;
      ProtectKernelModules = true;
      ProtectKernelTunables = true;
      RestrictAddressFamilies = [
        "AF_INET"
        "AF_UNIX"
      ];
      LockPersonality = true;
      MemoryDenyWriteExecute = true;
      CapabilityBoundingSet = "";
    };
  };

  # Wolf uses host networking. Expose only its documented GameStream ports;
  # there is no blanket trusted interface or Docker-published-port bypass.
  networking.firewall = {
    allowedTCPPorts = [
      47984 # HTTPS
      47989 # HTTP / pairing
      48010 # RTSP
    ];
    allowedUDPPorts = [
      47999 # control
      48100 # video
      48200 # audio
    ];
    # VBAN carries raw microphone audio and has no authentication layer. Keep
    # it on the physical LAN and accept packets only from this Mac; do not add
    # 6980 to the broad GameStream allowlist above.
    extraCommands = ''
      iptables -w -A nixos-fw \
        -i eth0 \
        -s ${vbanClientAddress}/32 \
        -p udp \
        --dport ${toString vbanPort} \
        -m comment --comment "VBAN microphone from Mac" \
        -j nixos-fw-accept
    '';
    extraStopCommands = ''
      iptables -w -D nixos-fw \
        -i eth0 \
        -s ${vbanClientAddress}/32 \
        -p udp \
        --dport ${toString vbanPort} \
        -m comment --comment "VBAN microphone from Mac" \
        -j nixos-fw-accept \
        2>/dev/null || true
    '';
  };
}
