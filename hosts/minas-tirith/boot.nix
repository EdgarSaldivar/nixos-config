{ config, lib, pkgs, ... }: {
   # Use the systemd-boot boot loader.
  boot.loader.systemd-boot.enable = lib.mkForce true;
  boot.loader.systemd-boot.configurationLimit = 10;
  boot.loader.systemd-boot.editor = false;
  boot.loader.efi.canTouchEfiVariables = true;
  boot.loader.efi.efiSysMountPoint = "/boot/efi";
  boot.tmp.cleanOnBoot = true;


  # This is absolutely necessary to ensure you ssh into the PID cryptsetup-askpass rather than running a second leading to a loop. 
  boot.initrd.network.postCommands =
            let
              disk = "/dev/disk/by-partlabel/disk-my-disk-luks";
            in
            ''
              echo 'network postCommaxnds'
              echo 'cryptsetup-askpass || echo "Unlock was successful; exiting SSH session" && exit 1' >> /root/.profile

              devices="$( \
                ip --oneline link show up \
                  | sed -E 's/^[0-9]+:\s*(\w+):.*$/\1/' \
              )"
              ips="$( \
                ip --oneline addr show "$devices" \
                  | head -n1 \
                  | sed -E 's/.*\s+inet\s+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+).*/\1/' \
              )"
              echo "starting sshd at root@$ips:${toString config.boot.initrd.network.ssh.port}..."
            '';
      
  boot.supportedFilesystems = lib.mkForce [
    "vfat"
    "xfs"
    "cifs"
    "ntfs"
  ];

  boot.initrd.luks.forceLuksSupportInInitrd = true;
  #Ssh into luks at boot
  boot.kernelParams = [ "ip=dhcp" "net.ifnames=0" ];
  #boot.kernelModules = [ "virtio_pci" "vfat" "nls_cp437" "nls_iso8859-1" ];
  boot.initrd.network.enable = true;
  boot.initrd.network.ssh.enable = true;
  #boot.initrd.kernelModules = [ "virtio_pci" "vfat" "nls_cp437" "nls_iso8859-1" ];
  environment.systemPackages = with pkgs; [cryptsetup];
  boot.initrd.systemd.users.root.shell = "/bin/cryptsetup-askpass";
  boot.initrd.systemd.services.sshd.enable = true;

  
  # This includes the ssh keys of all users in the wheel group, can also just add specific ones too.
  # authorizedKeys = [ "ssh-rsa ..." ];
  #boot.initrd.network.ssh.authorizedKeys = with lib; concatLists (mapAttrsToList (name: user: if elem "wheel" user.extraGroups then user.openssh.authorizedKeys.keys else []) config.users.users);
  boot.initrd.network.ssh.authorizedKeys = ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA+PoI3q67ZKz5oWtHVWfKzIRyBagoaFqYu/TqndfqTW MacBook-Pro.localdomain-19-05-2022"];
  boot.initrd.network.ssh.hostKeys = [ /etc/ssh/ssh_host_ed25519_key ];
  
}