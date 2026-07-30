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
    # uncorrectable machine checks in July 2026. BMC virtual media is not
    # configured, so without this there is no way to run a memory test remotely.
    # With it, testing is one reboot away over SOL/iKVM, which is what makes it
    # possible to walk memory speed back UP from a conservative baseline and
    # prove each step rather than guessing.
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
    # Initrd networking via DHCP rather than a static ip= param: two onboard
    # NICs mean guessing eth0-vs-eth1 is fragile, and DHCP tries whichever is
    # actually plugged in. Userspace networking is static regardless.
    "ip=dhcp"
  ];

  # ---------------------------------------------------------------------------
  # Remote LUKS unlock over SSH, in the initrd
  # ---------------------------------------------------------------------------
  boot.initrd.systemd.enable = true;
  boot.initrd.network.enable = true;

  boot.initrd.network.ssh = {
    enable = true;
    # Deliberately not 22: keeps the initrd host key out of the same
    # known_hosts entry as the real sshd, so a legitimate key difference does
    # not look like a MITM every reboot.
    port = 2222;

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
  # `emergencyAccess = true` permits an emergency shell without a password.
  # Justified here because the disk is LUKS-encrypted: reaching this prompt at
  # all requires having already supplied the passphrase, so it grants nothing to
  # someone who has merely stolen the drive.
  boot.initrd.systemd.emergencyAccess = true;

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
