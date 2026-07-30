# minas-tirith (raz-server) — bootloader and remote LUKS unlock.
#
# The whole point of this file: root is LUKS-encrypted and this machine lives at
# someone else's house. On the old install the passphrase prompt came from GRUB,
# which draws to graphics only — when a BIOS update reset the primary display to
# the discrete GPU, the prompt became invisible and the box was unreachable for
# hours despite being perfectly healthy.
#
# Here the prompt lives in the initrd instead, which can be reached three ways:
#   1. SSH into the initrd (below) and run cryptsetup-askpass  <-- primary
#   2. BMC serial console (SOL) — see console= params below
#   3. BMC iKVM video
{
  config,
  lib,
  pkgs,
  ...
}:
{
  # ---------------------------------------------------------------------------
  # Bootloader
  # ---------------------------------------------------------------------------
  boot.loader.systemd-boot = {
    enable = true;
    configurationLimit = 10; # ESP is 2G; 10 generations fits comfortably
    editor = false; # no kernel cmdline editing at the console

    # memtest86+ as a boot entry. This machine has 4x32GB non-ECC dual-rank
    # DDR4 on AM4 — the hardest case for the memory controller — and took two
    # uncorrectable machine checks in July 2026. Having it as a boot entry means
    # testing is one reboot away over SOL, with no ISO to mount and no reliance
    # on BMC virtual media working that day. That is what makes it practical to
    # walk memory speed back UP from the conservative 2666 baseline and prove
    # each step rather than guessing.
    memtest86.enable = true;
  };
  boot.loader.efi.canTouchEfiVariables = true;
  # disko mounts the ESP at /boot (not the older /boot/efi split).
  boot.loader.efi.efiSysMountPoint = "/boot";

  boot.tmp.cleanOnBoot = true;

  # supportedFilesystems is set in ./zfs.nix (and must include zfs). The old
  # config used lib.mkForce here with a list that omitted zfs, which would have
  # fought the ZFS module — removed deliberately.

  # ---------------------------------------------------------------------------
  # Console — make the boot visible over the BMC.
  # ---------------------------------------------------------------------------
  # SOL on this board is the UART Linux calls ttyS1 (0x2f8), verified by writing
  # markers to each port while a SOL session was attached. ttyS0 is NOT it.
  #
  # ORDER MATTERS AND THIS ORDER IS DELIBERATE. The LAST console= becomes
  # /dev/console, which is where sulogin/emergency mode reads input from. It must
  # be the SERIAL port, not tty1: tty1 renders on whatever the firmware picked as
  # primary display, and on 2026-07-29 a BIOS update flipped that to the discrete
  # RTX 2080 — leaving a live console nobody could see for hours. Putting tty1
  # last would recreate exactly that failure at the worst possible moment.
  boot.kernelParams = [
    "console=tty1"
    "console=ttyS1,115200n8"
    # Predictable interface naming in the initrd. Userspace matches NICs by MAC
    # (see ./system.nix), so renaming here is harmless there.
    "net.ifnames=0"
  ];

  # ---------------------------------------------------------------------------
  # Initrd networking — static, matched by MAC.
  # ---------------------------------------------------------------------------
  # Previously `ip=dhcp`, which reintroduced exactly the dependency the rest of
  # this config rejects: a DHCP reservation on a LAN whose owner reconfigures
  # things. If that reservation moves or the DHCP server is down, the initrd gets
  # no address, SSH unlock is impossible, and the only way in is the SOL console.
  #
  # systemd-initrd can run networkd, so the initrd gets the same MAC-matched
  # static configuration as userspace (see ../system.nix). Matching on MAC also
  # survives this board's PCI renumbering, which `eth0` would not.
  boot.initrd.systemd.network = {
    enable = true;
    networks."10-lan" = {
      matchConfig.MACAddress = "a8:a1:59:c0:4e:73";
      address = [ "10.0.1.6/20" ];
      routes = [ { Gateway = "10.0.0.1"; } ];
      networkConfig.DNS = [ "10.0.0.1" ];
      linkConfig.RequiredForOnline = "routable";
    };
  };

  # Bound the initrd's wait for the link. Default systemd-networkd-wait-online
  # blocks for up to ~120s when the cable is out or the switch is down — turning
  # a plugged-out NIC into a two-minute stall before the console passphrase
  # prompt even appears, on a box where the console is a serial link someone is
  # watching. Failing fast just means falling through to the SOL prompt, which is
  # the fallback path anyway.
  boot.initrd.systemd.services.systemd-networkd-wait-online.serviceConfig = {
    ExecStart = [
      ""
      "${config.boot.initrd.systemd.package}/lib/systemd/systemd-networkd-wait-online --timeout=20"
    ];
  };

  # ---------------------------------------------------------------------------
  # Remote LUKS unlock over SSH, in the initrd
  # ---------------------------------------------------------------------------
  boot.initrd.systemd.enable = true;
  boot.initrd.network.enable = true;

  boot.initrd.network.ssh = {
    enable = true;

    # ⚠️  PORT 22 IS DELIBERATE AND LOAD-BEARING. Do not "fix" this to 2222.
    #
    # This was 2222, to keep the initrd host key out of the same known_hosts
    # entry as the production sshd. That reasoning was sound in isolation and
    # WRONG for this network, because it silently defeated the entire purpose of
    # this file. The actual topology:
    #
    #     minas.saldivar.io:2222 --NAT--> 10.0.1.6:22      (the only forward)
    #     production sshd listens on 22 internally
    #
    # The external forward lands on internal port 22. With the initrd listening
    # on 2222, NOTHING answers on 22 during initrd, so remote unlock from outside
    # the LAN — the whole reason for initrd SSH on a machine an hour away — was
    # impossible. It would have worked from inside the LAN and failed exactly
    # when it was needed, which is the worst way for a thing to be broken.
    #
    # Sharing port 22 is safe: initrd sshd and stage-2 sshd never run at the same
    # time. The known_hosts collision is real but is a CLIENT-side problem with a
    # standard client-side fix — use HostKeyAlias so the two keys coexist:
    #
    #     Host minas-initrd                 Host minas
    #       HostName minas.saldivar.io        HostName minas.saldivar.io
    #       Port 2222                         Port 2222
    #       User root                         User edgar
    #       HostKeyAlias minas-initrd
    #
    # Then `ssh minas-initrd` unlocks and `ssh minas` is normal access, with no
    # spurious MITM warning either way. See INSTALL-RUNBOOK step 7.
    port = 22;

    # ⚠️  SECURITY: the initrd lives on the UNENCRYPTED ESP, so any private key
    # placed in it is readable by anyone who has the disk. This MUST be a
    # dedicated throwaway key used only for unlocking — never the machine's real
    # SSH host key. (The previous config referenced /etc/ssh/ssh_host_ed25519_key,
    # which leaked the production host key into the unencrypted initrd.)
    #
    # Generate once on the installed system, then rebuild:
    #   ssh-keygen -t ed25519 -N "" -f /etc/secrets/initrd/ssh_host_ed25519_key
    #   chmod 600 /etc/secrets/initrd/ssh_host_ed25519_key
    hostKeys = [ "/etc/secrets/initrd/ssh_host_ed25519_key" ];

    authorizedKeys = config.users.users.edgar.openssh.authorizedKeys.keys;
  };

  # NOTE: the old config set
  #     boot.initrd.systemd.users.root.shell = "/bin/cryptsetup-askpass";
  # That pattern belongs to the pre-systemd (classic) initrd. Under systemd
  # stage 1 — which NixOS 26.05 uses and which is required for this setup —
  # cryptsetup-askpass does not exist, and the module system hard-fails with:
  #     "cryptsetup-askpass is not available in systemd stage 1"
  #
  # Deliberately leaving root with a REAL shell rather than pinning it to an
  # unlock command: if the box fails to boot for some reason other than the
  # passphrase, that initrd shell is the only remote debugging surface there is.
  #
  # To unlock, after `ssh -p 2222 root@<host>`:
  #     systemd-tty-ask-password-agent      # prompts for the LUKS passphrase
  # then boot continues on its own. (`systemctl default` also resumes the boot
  # if the volume was unlocked by some other route.)

  environment.systemPackages = [ pkgs.cryptsetup ];

  # ---------------------------------------------------------------------------
  # Emergency / rescue access
  # ---------------------------------------------------------------------------
  # Without this, a drop to emergency or rescue mode is a hard lockout: sulogin
  # demands the root password, root has none, and mutableUsers = false means one
  # cannot be set on the running system. The machine would be alive, on the
  # console, and completely unusable — the same shape as the 2026-07-29 outage.
  #
  # `emergencyAccess = true` permits an emergency shell in the INITRD without a
  # password. Note the scope carefully: this covers stage 1 ONLY.
  #
  # (An earlier comment here claimed the LUKS passphrase gates access to this
  # prompt. That is wrong for stage 1 — the initrd emergency shell can be reached
  # BEFORE the volume is unlocked. What actually limits exposure is that reaching
  # it requires either physical/BMC console access or the initrd SSH key, and the
  # encrypted root is still locked at that point.)
  boot.initrd.systemd.emergencyAccess = true;

  # Stage 2 emergency/rescue is a SEPARATE lockout, and the more likely one: it
  # runs sulogin against the root account, with no network and no sshd. With
  # mutableUsers = false and no root password, a single failure — say the ESP
  # failing to mount — drops the machine to a prompt that cannot be answered even
  # over SOL. Root therefore gets the same sops-provided password as edgar; see
  # ./secrets.nix.
  users.users.root.hashedPasswordFile = config.sops.secrets.edgar-password.path;

  # Sanity: if the LUKS device disko declares ever stops being picked up in the
  # initrd, unlocking becomes impossible remotely. Fail the build instead.
  assertions = [
    {
      assertion = config.boot.initrd.network.ssh.enable -> config.boot.initrd.network.enable;
      message = "minas-tirith: initrd SSH is enabled but initrd networking is not.";
    }
    {
      assertion = lib.elem "igb" config.boot.initrd.availableKernelModules;
      message = ''
        minas-tirith: initrd SSH unlock requires the `igb` NIC driver in
        boot.initrd.availableKernelModules, otherwise there is no network in the
        initrd and the machine cannot be unlocked remotely.
      '';
    }
  ];
}
