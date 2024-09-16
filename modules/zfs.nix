{ config, pkgs, ... }: {
  boot.supportedFilesystems = [ "zfs" ];
  boot.extraModulePackages = [ config.boot.zfs.package ];
  boot.initrd.supportedFilesystems = [ "zfs" ];
  boot.kernelPackages = config.boot.zfs.package.latestCompatibleLinuxPackages;
  boot.kernelParams = [ "nohibernate" ];

  # Prompt to Import Encrypted Zpool at boot
  boot.zfs.forceImportAll = true;
  boot.zfs.extraPools = [ "1464522621344907536" ];

  services.zfs.trim.enable = true;
  services.zfs.autoScrub.enable = true;

  systemd.services.zpool-import = {
  description = "Import ZFS pool at boot";
  after = [ "zfs-import-scan.service" ];
  wantedBy = [ "multi-user.target" ];
  serviceConfig = {
    ExecStart = "${pkgs.sudo}/bin/sudo ${pkgs.zfs}/bin/zpool import -a";
    Type = "oneshot";
    RemainAfterExit = true;
  };
};

}