# edoras — Wi-Fi, so the box can run with no cable (Intel AX200, iwlwifi).
#
# ⛔ NOBODY ON SITE CAN HELP. Once the cable is pulled this link is the only way
# in, so everything here is about coming back by itself: the cable still wins
# while it is plugged in, power saving is off, wpa_supplicant restarts if it
# dies, and a watchdog restarts the stack and then reboots if connectivity stays
# lost.
#
# The SSID and passphrase are never in this public repo or the Nix store. They
# live only in /etc/wpa_supplicant.conf on edoras, which the owner writes over
# the tailnet: network={...} blocks and nothing else (no ctrl_interface lines;
# see wpaConf). See docs/runbooks/edoras/install.md, section 10.
{ lib, pkgs, ... }:
let
  # A STRING, not a Nix path literal. A path literal would be copied into the
  # world-readable store at evaluation, passphrase included.
  wpaConf = "/etc/wpa_supplicant.conf";

  # Connectivity watchdog. Each minute: does the gateway of some IPv4 default
  # route answer ping or ARP on that route's interface? While the cable works,
  # its route answers, so this never acts.
  #
  #   lost >= 5 min:  restart wpa_supplicant and systemd-networkd, then again
  #                   every 5 min while it stays lost;
  #   lost >= 30 min: reboot.
  #
  # Time is uptime (monotonic), and the state is in /run, so a reboot starts
  # from zero: a router that stays down means one reboot per ~30 min. That is
  # deliberate: nobody there can do anything better. Every action, and the
  # recovery, goes to the journal (`journalctl -u wifi-watchdog`).
  watchdog = pkgs.writeShellApplication {
    name = "wifi-watchdog";
    runtimeInputs = with pkgs; [
      coreutils
      gawk
      iproute2
      iputils
      iw
      systemd
    ];
    text = ''
      restart_after=300
      reboot_after=1800
      state=/run/wifi-watchdog
      mkdir -p "$state"
      now=$(cut -d. -f1 /proc/uptime)

      healthy() {
        local gw dev
        while read -r gw dev; do
          if ping -q -c 2 -W 2 -I "$dev" "$gw" >/dev/null 2>&1 \
            || arping -q -c 2 -w 4 -I "$dev" "$gw" >/dev/null 2>&1; then
            return 0
          fi
        done < <(ip -4 route show default | awk '{
          g = ""; d = ""
          for (i = 1; i < NF; i++) { if ($i == "via") g = $(i + 1); if ($i == "dev") d = $(i + 1) }
          if (g != "" && d != "") print g, d
        }')
        return 1
      }

      snapshot() {
        echo "  routes: $(ip -4 route show default | tr '\n' ';')"
        echo "  links: $(ip -br -4 addr | tr -s ' ' | tr '\n' ';')"
        for w in /sys/class/net/*/wireless; do
          [ -e "$w" ] || continue
          w=''${w%/wireless}; w=''${w##*/}
          echo "  $w: $(iw dev "$w" link 2>&1 | head -n 3 | tr -s ' \t' ' ' | tr '\n' ';')"
        done
      }

      if healthy; then
        if [ -f "$state/down-since" ]; then
          echo "<5>connectivity back after $((now - $(cat "$state/down-since")))s"
          rm -f "$state/down-since" "$state/last-restart"
        fi
        exit 0
      fi

      if [ ! -f "$state/down-since" ]; then
        echo "$now" > "$state/down-since"
        echo "<4>no default route's gateway answers; restart at ''${restart_after}s, reboot at ''${reboot_after}s if it stays so"
        snapshot
        exit 0
      fi

      since=$(cat "$state/down-since")
      down=$((now - since))
      if [ "$down" -ge "$reboot_after" ]; then
        echo "<2>no connectivity for $((down / 60)) min: rebooting"
        snapshot
        systemctl reboot
        exit 0
      fi
      last=$(cat "$state/last-restart" 2>/dev/null || echo "$since")
      if [ "$down" -ge "$restart_after" ] && [ $((now - last)) -ge "$restart_after" ]; then
        echo "$now" > "$state/last-restart"
        echo "<3>no connectivity for $((down / 60)) min: restarting wpa_supplicant and systemd-networkd"
        snapshot
        systemctl restart wpa_supplicant.service systemd-networkd.service \
          || echo "<3>the restart failed (exit $?)"
      fi
    '';
  };
in
{
  networking.wireless = {
    enable = true;
    # No interface list: the module's single wpa_supplicant.service finds the
    # radio itself and its udev rule restarts it when one appears or goes, so
    # a PCI renumbering that renames wlp4s0 cannot strand the box (the same
    # reason ./system.nix matches en*, as osgiliath's wifi.nix does).
    #
    # userControlled gives the control socket the patched wpa_cli expects,
    # /run/wpa_supplicant/control, so `sudo wpa_cli status` works. Nobody is
    # in the wpa_supplicant group. It also makes the module use its generated
    # nixos.conf as the main file; imperative mode would read
    # /etc/wpa_supplicant/imperative.conf instead, and chmod it 664.
    userControlled = true;
    extraConfig = "country=US";
    # Loaded with -I next to nixos.conf (the upstream NixOS test "basic"
    # covers this). A ctrl_interface_group= line in this file would make the
    # daemon exit at start: it runs as the wpa_supplicant user and cannot
    # chown its socket directory to another group.
    extraConfigFiles = [ wpaConf ];
  };

  # The hardened daemon runs as wpa_supplicant and must be able to read the
  # file the owner writes. A "+" ExecStartPre can't fix the mode: inside the
  # unit's mount namespace /etc is read-only even for "+" commands (seen on
  # 2026-11-26: chown -> EROFS, so the unit never started). tmpfiles sets it
  # instead, at boot and on every switch; put-wifi-password.zsh writes it
  # root:wpa_supplicant 0640 directly as well.
  systemd.tmpfiles.settings."10-wpa-supplicant-psk"."${wpaConf}".z = {
    mode = "0640";
    user = "root";
    group = "wpa_supplicant";
  };

  systemd.services.wpa_supplicant = {
    serviceConfig = {
      Restart = "always";
      RestartSec = "30s";
    };
    # Never stop retrying.
    startLimitIntervalSec = 0;
  };

  # DHCP on the radio, IPv4 and IPv6, with every route worse than the cable's
  # (DHCPv4 1024, and 512:1024:2048 for RA routes by preference; this router
  # announces medium), so the cable carries traffic while it is plugged in and
  # the radio takes over the moment its routes go. Either link being routable
  # counts as online (wait-online.anyInterface in ./system.nix).
  systemd.network.networks."20-wifi" = {
    matchConfig.Name = "wl*";
    networkConfig.DHCP = "yes";
    dhcpV4Config.RouteMetric = 2048;
    ipv6AcceptRAConfig.RouteMetric = 2048;
    linkConfig.RequiredForOnline = "routable";
  };

  # Two interfaces on one subnet. By default Linux answers ARP for any of its
  # addresses on either interface and can source ARP from the other one's
  # address, so the router may learn the cable's IP at the radio's MAC. The
  # strict reverse-path filter would then drop that traffic, because the
  # route back is the cable. Answer ARP only for the interface's own address
  # and announce from it; and filter loosely, the usual setting for a
  # multi-homed host, so the moments when both links carry traffic (cable
  # pulled or plugged back in) are not dropped either.
  boot.kernel.sysctl = {
    "net.ipv4.conf.all.arp_ignore" = 1;
    "net.ipv4.conf.all.arp_announce" = 2;
  };
  networking.firewall.checkReversePath = "loose";

  # Power saving is the usual cause of iwlwifi dropping off. The firmware
  # stays awake (iwlmvm power_scheme 1, "always active"; the default is 2,
  # balanced), and mac80211's own power save is switched off as the radio
  # appears. The module options take effect when iwlwifi next loads (boot).
  boot.extraModprobeConfig = ''
    options iwlwifi power_save=0
    options iwlmvm power_scheme=1
  '';
  services.udev.extraRules = ''
    ACTION=="add", SUBSYSTEM=="net", KERNEL=="wl*", RUN+="${pkgs.iw}/bin/iw dev $name set power_save off"
  '';

  systemd.services.wifi-watchdog = {
    description = "Restart Wi-Fi, then reboot, if connectivity stays lost";
    serviceConfig = {
      Type = "oneshot";
      ExecStart = lib.getExe watchdog;
      TimeoutStartSec = "2min";
    };
  };
  systemd.timers.wifi-watchdog = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "2min";
      OnUnitActiveSec = "1min";
      AccuracySec = "10s";
    };
  };

  # Diagnosis on a box nobody can look at: `iw dev wlp4s0 link` (association
  # and signal), `sudo wpa_cli status`. networking.wireless installs
  # wpa_supplicant too; listed here as well so the tools stay if it goes.
  environment.systemPackages = [
    pkgs.iw
    pkgs.wpa_supplicant
  ];
}
