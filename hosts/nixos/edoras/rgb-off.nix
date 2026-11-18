# edoras — every RGB LED the host can reach, off, on every boot.
#
# Found 2026-11-17 by OpenRGB 1.0rc3 (`openrgb --noautoconnect --list-devices`
# as root) and switched off by hand with the exact commands below:
#
#   Z590 AORUS ULTRA   RGB Fusion 2: ITE IT5701, USB 048d:5702. Zones D_LED1
#                      and D_LED2 (the two addressable headers), I/O Cover,
#                      LED_C1 and LED_C2 (the two 12 V RGB headers). It has no
#                      Off mode, so: Static, black, brightness 0.
#   G.Skill Trident Z RGB, both DIMMs   ENE SMBus controllers on the chipset
#                      SMBus (i2c-i801) at 0x71 and 0x73. Off mode.
#
# Not reachable, and still lit:
#   RTX 3090   Gigabyte subsystem 1458:4041, VBIOS 94.02.59.00.60, which
#              TechPowerUp lists as the AORUS RTX 3090 XTREME. OpenRGB has no
#              detector for that subsystem, and nothing answered at 0x60-0x77 on
#              any of its NVIDIA I2C buses (read-only i2cdetect, 2026-11-17).
#              Guessing a protocol blind at a remote card that is serving
#              inference is not worth it. Its own lighting stays as it was.
#   RTX A4000  has no RGB.
# Deliberately left alone: the I225-V port LEDs (igc-0300-led*). They are not
# RGB, and "is the light by the network cable on?" is the one question someone
# on site can answer.
#
# ⛔ THE ARGB HEADERS MUST BE SIZED. OpenRGB skips a strip zone with 0 LEDs, and
# both D_LED zones are detected with 0, so without `--size` whatever hangs on
# those headers keeps the firmware's rainbow. One LED is enough: in Static mode
# the controller paints the whole header with one colour. Zones 0 and 1 are
# D_LED1 and D_LED2 in OpenRGB's order.
#
# ⛔ THE ITE CONTROLLER CAN FAIL TO ENUMERATE. On the boot of 2026-11-17, after a
# power cut, the kernel logged "config index 0 descriptor too short" and "can't
# set config #1, error -71": the device sat on the bus with no HID interface,
# invisible to OpenRGB. Disabling and re-enabling its hub port re-enumerated it
# cleanly, so the script below does that, up to three times.
#
# None of this persists in the hardware: the board and the DIMMs return to
# their stored effects at every power-on, which is why this runs every boot.
# POST and the boot loader stay lit until it does.
#
# Verify after a boot:
#   journalctl -b -u rgb-off
#   sudo nix shell nixpkgs#openrgb -c openrgb --noautoconnect --list-devices
# which should show [Off] on both ENE DRAM (it leaves its logs in
# /root/.config/OpenRGB). The DIMMs report their mode back; the board does not,
# so only someone looking at it can confirm the board side.
{ lib, pkgs, ... }:
let
  rgbOff = pkgs.writeShellApplication {
    name = "rgb-off";
    runtimeInputs = [
      pkgs.coreutils
      pkgs.gnugrep
      pkgs.openrgb
    ];
    text = ''
      # OpenRGB's config (zone sizes among it) lives in the runtime directory
      # and is gone when the unit stops: every boot starts from detection.
      # ⛔ HOME too: OpenRGB's logger ignores --config and writes under
      # $HOME/.config/OpenRGB/logs, or ./logs with no HOME, which a system
      # unit does not have and ProtectSystem makes read-only.
      cfg="''${RUNTIME_DIRECTORY:-$(mktemp -d)}"
      export HOME="$cfg"
      cd "$cfg"

      # A missing device or zone exits 255, but a mode the device lacks only
      # prints "Error: Mode ... not available" and applies its first mode with
      # exit 0, so a line starting "Error" counts as failure too. The warning
      # about absent udev rules matters only to non-root users and is dropped.
      orgb() {
        local out status=0
        out="$(openrgb --noautoconnect --config "$cfg" "$@" 2>&1)" || status=$?
        printf '%s\n' "$out" | grep -v -e 'udev rules' || true
        [ "$status" -eq 0 ] && ! printf '%s\n' "$out" | grep -q '^Error'
      }

      ite_hidraw() {
        grep -qsi '^HID_ID=0003:0000048D:00005702$' /sys/class/hidraw/hidraw*/device/uevent
      }

      ite_usb() {
        local d
        for d in /sys/bus/usb/devices/*; do
          if [ "$(cat "$d/idVendor" 2>/dev/null)" = 048d ] &&
            [ "$(cat "$d/idProduct" 2>/dev/null)" = 5702 ]; then
            echo "$d"
            return 0
          fi
        done
        return 1
      }

      # Wait up to 10 s for the board controller's HID interface; if it never
      # comes, re-enumerate the controller by cycling its hub port.
      board=0
      resets=0
      while :; do
        for _ in $(seq 20); do
          if ite_hidraw; then
            board=1
            break 2
          fi
          sleep 0.5
        done
        if [ "$resets" -ge 3 ]; then
          break
        fi
        if ! dev="$(ite_usb)"; then
          echo "<3>rgb-off: the board's RGB controller (ITE 048d:5702) is not on the USB bus"
          break
        fi
        resets=$((resets + 1))
        echo "<4>rgb-off: ITE 048d:5702 at ''${dev##*/} has no HID interface; cycling its USB port ($resets of 3)"
        # ⛔ Resolve the hub's port first. Disabling it disconnects the device,
        # and $dev, with its `port` link, vanishes with it: re-enabling through
        # that path fails and leaves the port off (seen in testing, 2026-11-17).
        port="$(readlink -f "$dev/port")"
        echo 1 > "$port/disable"
        sleep 2
        echo 0 > "$port/disable"
      done

      failed=0
      orgb --device "ENE DRAM" --mode off || failed=1
      if [ "$board" -eq 1 ]; then
        orgb --device "AORUS" \
          --zone 0 --size 1 --zone 1 --size 1 \
          --mode static --color 000000 --brightness 0 || failed=1
      else
        echo "<3>rgb-off: board lighting left on: its RGB controller never enumerated"
        failed=1
      fi
      exit "$failed"
    '';
  };
in
{
  # i2c-dev gives OpenRGB /dev/i2c-*; i2c-i801 is the chipset SMBus the DIMMs
  # sit on. udev would load i2c-i801 anyway, but only eventually; listing it
  # here puts it ahead of the unit through systemd-modules-load.
  boot.kernelModules = [
    "i2c-dev"
    "i2c-i801"
  ];

  # A oneshot rather than services.hardware.openrgb: that runs OpenRGB as a
  # resident SDK server listening on the network, which a host that only ever
  # wants the lights off has no use for.
  systemd.services.rgb-off = {
    description = "Turn off the board and DIMM RGB lighting";
    wantedBy = [ "multi-user.target" ];
    after = [
      "systemd-modules-load.service"
      "systemd-udev-trigger.service"
    ];
    # A failed run (a controller that did not answer) tries again; four
    # attempts, then the unit stays failed where `systemctl --failed` shows it.
    startLimitIntervalSec = 600;
    startLimitBurst = 4;
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = lib.getExe rgbOff;
      Restart = "on-failure";
      RestartSec = 30;
      SyslogLevelPrefix = true;
      RuntimeDirectory = "rgb-off";
      # Root without capabilities: /dev/hidraw*, /dev/i2c-* and the hub port's
      # `disable` file are all owned by root, so plain file permissions are
      # what it needs. /sys stays writable for that file; nothing else does.
      CapabilityBoundingSet = [ "" ];
      DevicePolicy = "closed";
      DeviceAllow = [
        "char-hidraw rw"
        "char-i2c rw"
      ];
      NoNewPrivileges = true;
      ProtectSystem = "strict";
      ProtectHome = true;
      PrivateTmp = true;
      PrivateNetwork = true;
      RestrictAddressFamilies = [
        "AF_UNIX"
        "AF_NETLINK"
      ];
    };
  };
}
