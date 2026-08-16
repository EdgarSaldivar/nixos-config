{
  lib,
  pkgs,
  nixosConfigurations,
  darwinConfigurations,
  ...
}:

# Disk encryption is useful on this headless host only while unattended
# unlock and both manual recovery paths remain wired exactly as designed.
let
  cfg = nixosConfigurations.nardol.config;
  initrd = cfg.boot.initrd;
  lan = initrd.systemd.network.networks."10-nardol-lan";
  ssh = initrd.network.ssh;
  foreignPvFilter = ''devices/global_filter = [ "r|.*|" ]'';
  stage2LvmConfig = cfg.environment.etc."lvm/lvm.conf".text;
  initrdLvmConfig = initrd.systemd.contents."/etc/lvm/lvm.conf".text;
  usbKeyDevice = "/dev/disk/by-id/usb-General_USB_Flash_Disk_0305500000000280-0:0";
in
if !initrd.clevisLuksAskpass.enable || !initrd.clevisLuksAskpass.useTang then
  throw "nardol unattended LUKS unlock must use Clevis Tang askpass"
else if
  !initrd.systemd.enable
  || !initrd.systemd.network.enable
  || initrd.network.enable
  || initrd.systemd.network.networks."99-ethernet-default-dhcp".enable
  || initrd.systemd.network.networks."99-wireless-client-dhcp".enable
then
  throw "nardol unlock must use only systemd networking in the initrd"
else if
  lan.matchConfig.MACAddress != "9c:6b:00:36:e0:e8"
  # Nardol travels: the initrd takes a DHCP address so it works on a
  # foreign LAN, but must never accept routing or resolution from one.
  # All four of these are required -- UseGateway covers only the Router
  # option, UseRoutes covers option-121 classless routes, IPv6AcceptRA
  # covers router advertisements, and lan.routes covers declarative
  # ones. Dropping any single check leaves a way in.
  || lan.address != [ ]
  || lan.routes != [ ]
  || lan.networkConfig.DHCP != "ipv4"
  || lan.networkConfig.IPv6AcceptRA != false
  || lan.networkConfig.LinkLocalAddressing != "no"
  || lan.dhcpV4Config.UseGateway != false
  || lan.dhcpV4Config.UseRoutes != false
  || lan.dhcpV4Config.UseDNS != false
  || lan.dhcpV4Config.ClientIdentifier != "mac"
  # Off-site discovery is via the foreign router's lease table; this is
  # the label it appears under, so a silent removal breaks the runbook.
  || lan.dhcpV4Config.Hostname != "nardol-initrd"
then
  throw "nardol initrd must take a DHCP address only, with no route, DNS, or RA"
else if !lib.elem "igb" initrd.availableKernelModules then
  throw "nardol initrd is missing the Intel I211 igb driver"
else if
  !lib.all (m: lib.elem m initrd.availableKernelModules) [
    "usb_storage"
    "xhci_pci"
    "sd_mod"
  ]
then
  throw "nardol initrd is missing the USB modules needed to read the unlock key"
else if
  let
    luksNames = [
      "nardol-root"
      "nardol-fast"
    ];
    dev = name: cfg.boot.initrd.luks.devices.${name};
  in
  !lib.all (
    name:
    (dev name).keyFile == usbKeyDevice
    && (dev name).keyFileSize == 4096
    && (dev name).keyFileOffset == 4194304
    && (dev name).keyFileTimeout != null
    && (dev name).keyFileTimeout > 0
    && !(dev name).fallbackToPassword
  ) luksNames
then
  # keyFileTimeout is what makes a MISSING stick survivable: without it
  # systemd hard-depends on the by-id device and the boot hangs with no
  # console or SSH recovery. fallbackToPassword must stay false --
  # systemd stage 1 implies it and nixpkgs asserts it.
  throw "nardol USB unlock key contract changed on one or both volumes"
else if
  !cfg.services.lvm.enable
  || !lib.hasInfix foreignPvFilter stage2LvmConfig
  || !lib.hasInfix foreignPvFilter initrdLvmConfig
then
  throw "nardol must keep LVM udev support while rejecting all foreign PV scanning"
else if
  !ssh.enable
  || ssh.port != 2222
  || ssh.hostKeys != [ "/etc/secrets/initrd/ssh_host_ed25519_key" ]
  || !lib.all (lib.hasInfix ''command="/bin/systemd-tty-ask-password-agent"'') ssh.authorizedKeys
  # Each forced option asserted individually: a single substring match
  # tolerates a key that quietly drops one of the others.
  || !lib.all (
    key:
    lib.all (opt: lib.hasInfix opt key) [
      "no-agent-forwarding"
      "no-port-forwarding"
      "no-X11-forwarding"
      "no-user-rc"
      ''from="''
    ]
  ) ssh.authorizedKeys
  # Key-only. The source restriction is now RFC1918-wide so a laptop at
  # a LAN party can reach the prompt, which makes these the real control.
  || !lib.all (d: lib.hasInfix d ssh.extraConfig) [
    "PasswordAuthentication no"
    "KbdInteractiveAuthentication no"
    "AuthenticationMethods publickey"
  ]
then
  throw "nardol restricted initrd SSH recovery contract changed"
else
  pkgs.runCommand "nardol-unlock-contract-ok" { } "touch $out"
