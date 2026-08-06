# osgiliath — hardware-backed services and preserved Frigate storage.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  frigateDevice = "/dev/disk/by-id/usb-SABRENT_SABRENT_DD5641988396E-0:0-part1";
  frigateMount = "/mnt/frigate-usb";
  frigateMountUnit = "mnt-frigate\\x2dusb.mount";
in
{
  hardware = {
    bluetooth = {
      enable = true;
      powerOnBoot = true;
    };
    coral.usb.enable = true;
    rasdaemon.enable = true;
  };
  users.users.edgar.extraGroups = [ "coral" ];

  # The USB Coral (18d1:9302) uses libedgetpu/udev support, not the gasket/apex
  # modules for the PCIe model. Prevent USB autosuspend from dropping it during
  # inference; no workload is enabled in this foundation slice.
  services.udev.extraRules = ''
    ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="18d1", ATTR{idProduct}=="9302", TEST=="power/control", ATTR{power/control}="on"
  '';

  services.smartd = {
    enable = true;
    autodetect = true;
  };
  services.fstrim = {
    enable = true;
    interval = "weekly";
  };

  zramSwap = {
    enable = true;
    memoryPercent = 25;
  };

  services.journald.extraConfig = ''
    SystemMaxUse=500M
    RuntimeMaxUse=100M
    MaxRetentionSec=1month
  '';

  # This exact partition contains the existing ext4 filesystem labelled
  # "frigate" and roughly 331 GiB of recordings. It is intentionally absent
  # from disko: installation may partition only the internal NVMe.
  #
  # `nofail` is intentionally absent. A missing or broken recording disk must
  # fail local-fs.target rather than making /mnt/frigate-usb look writable while
  # writes actually land on the root NVMe.
  fileSystems.${frigateMount} = {
    device = frigateDevice;
    fsType = "ext4";
    options = [
      "noatime"
      "nodev"
      "nosuid"
      "x-systemd.device-timeout=30s"
    ];
  };

  # LATER WORKLOAD SLICE HOOK: every Frigate-related service must include
  #   requires = [ "frigate-storage.target" ];
  #   after = [ "frigate-storage.target" ];
  # This target starts only after verifying that the exact preserved partition,
  # its ext4 filesystem, and its label are mounted at the intended path.
  systemd.targets.frigate-storage = {
    description = "Verified preserved Frigate storage";
    wantedBy = [ "multi-user.target" ];
    requires = [ "frigate-storage-verify.service" ];
    after = [ "frigate-storage-verify.service" ];
  };

  systemd.services.frigate-storage-verify = {
    description = "Verify preserved Frigate storage is mounted safely";
    requires = [ frigateMountUnit ];
    after = [ frigateMountUnit ];
    before = [ "frigate-storage.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      set -eu

      source="$(${pkgs.util-linux}/bin/findmnt --raw --noheadings --mountpoint ${frigateMount} --output SOURCE)"
      actual="$(${pkgs.coreutils}/bin/readlink -f "$source")"
      expected="$(${pkgs.coreutils}/bin/readlink -f ${frigateDevice})"
      fstype="$(${pkgs.util-linux}/bin/findmnt --raw --noheadings --mountpoint ${frigateMount} --output FSTYPE)"
      label="$(${pkgs.util-linux}/bin/blkid --match-tag LABEL --output value "$actual")"

      if [ "$actual" != "$expected" ]; then
        echo "ABORT: ${frigateMount} is backed by $actual, expected $expected" >&2
        exit 1
      fi
      if [ "$fstype" != "ext4" ] || [ "$label" != "frigate" ]; then
        echo "ABORT: ${frigateMount} must be the ext4 filesystem labelled frigate" >&2
        exit 1
      fi
    '';
  };

  assertions = [
    {
      assertion = config.fileSystems.${frigateMount}.device == frigateDevice;
      message = "osgiliath: /mnt/frigate-usb must use the exact preserved Sabrent partition";
    }
    {
      assertion = config.fileSystems.${frigateMount}.fsType == "ext4";
      message = "osgiliath: the preserved Frigate filesystem must remain ext4";
    }
    {
      assertion = !(lib.elem "nofail" config.fileSystems.${frigateMount}.options);
      message = "osgiliath: the Frigate mount must fail closed; nofail is forbidden";
    }
  ];
}
