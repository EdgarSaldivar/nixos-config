# imladris — identity, administrability, and microSD write reduction.
{ pkgs, ... }:
{
  # ---------------------------------------------------------------------------
  # Administrability — pelargir's 2026-08-04 lesson, applied in advance.
  # ---------------------------------------------------------------------------
  # pelargir's first install booted perfectly and was then IMPOSSIBLE to
  # administer: users/edgar sets mutableUsers = false, that host set no password,
  # sshd has PermitRootLogin = "no", and NixOS defaults wheelNeedsPassword to
  # true. sudo prompted for a password that did not exist, the console login
  # could not be satisfied either, and recovery meant physically unseating the
  # NVMe. minas-tirith documents the same trap at length; pelargir walked into it
  # anyway, because the config that DID set wheelNeedsPassword was deleted in a
  # rewrite.
  #
  # Two independent ways in, on purpose:
  #   1. passwordless sudo for wheel — remote admin over SSH keys (./default.nix)
  #   2. a real console password — a keyboard still works when sshd, networking
  #      or the tailnet is broken
  #
  # ✅ BOTH WAYS EXIST as of 2026-09-11. ./secrets.nix is imported and supplies
  # users.users.edgar.hashedPasswordFile from sops, so the console login is real.
  # Verify after any reinstall — this is the check pelargir's rebuild skipped.
  security.sudo.enable = true;

  # ---------------------------------------------------------------------------
  # microSD write reduction.
  # ---------------------------------------------------------------------------
  # Root lives on a card, and cards die of write amplification rather than age.
  # The single most important mitigation is NOT here — it is in ./storage.nix,
  # which puts Jellyfin's SQLite database, metadata and cache on NVMe. A media
  # server's database writes on every playback, scan and metadata update, and
  # that workload on a microSD is both the classic card-killer and the usual
  # cause of "Jellyfin feels sluggish on a Pi".
  #
  # What remains on the card is the Nix store and the OS: read-mostly, with
  # writes concentrated in `nixos-rebuild switch`. Builds happen on dol-amroth's
  # aarch64 builder, so that is closure copy traffic rather than compilation.

  # Swap on a microSD would be the worst possible write pattern. zram trades a
  # little CPU for compressed RAM instead, same as pelargir.
  zramSwap = {
    enable = true;
    memoryPercent = 25;
  };

  services.journald.extraConfig = ''
    SystemMaxUse=500M
  '';

  # ⚠️ UNVERIFIED FOR THE ENCLOSURE. This trims every mounted filesystem that
  # advertises discard support. The microSD benefits, and that alone justifies
  # it. Whether discard actually reaches the NVMe drives THROUGH the ASM2464
  # bridge is not known — USB mass storage translates NVMe to SCSI, and discard
  # passthrough is bridge-dependent rather than a property of the drives. A
  # bridge that does not support it makes fstrim skip the filesystem harmlessly;
  # it does not make the trim silently succeed. Confirm during commissioning
  # rather than assuming the drives' own capabilities carry across the bridge.
  services.fstrim.enable = true;

  services.openssh = {
    # The install runbook places this pre-generated key before first boot;
    # sops-nix derives the machine age identity from the same stable key, so it
    # must exist BEFORE there is anything to decrypt.
    hostKeys = [
      {
        path = "/etc/ssh/ssh_host_ed25519_key";
        type = "ed25519";
      }
    ];
    settings = {
      KbdInteractiveAuthentication = false;
    };
  };

  # ---------------------------------------------------------------------------
  # The PCIe connector is deliberately unused.
  # ---------------------------------------------------------------------------
  # pelargir's single PCIe lane is permanently consumed by its boot NVMe. This
  # host boots from microSD specifically to keep that connector free — it is the
  # slot for a future NVMe system disk, should the card's write endurance or
  # random-I/O latency ever become the complaint.
  #
  # It is NOT reserved for an AI HAT+. That board is a Hailo-8/8L vision
  # accelerator: no LLM support (that is the 10H in the AI HAT+ 2), no Immich
  # acceleration (Immich publishes armnn/cuda/rocm/openvino/rknn images, and
  # Hailo is absent), and it is not a video codec engine so it does nothing for
  # Jellyfin. Its one turnkey use is Frigate object detection, and osgiliath
  # already has a USB Coral and a preserved recordings disk configured for that.

  environment.systemPackages = with pkgs; [
    # Reading NVMe health through the enclosure needs the vendor passthrough:
    #   smartctl -d sntasmedia -i /dev/sdX
    # Auto-detection resolves to `-d sat`, which fails outright on this bridge.
    smartmontools
    # mergerfs supplies mount.fuse.mergerfs, which ./storage.nix's union mount
    # resolves at mount time.
    mergerfs
    # Present for enclosure triage: which bay reset, at what link speed.
    usbutils

    # ⛔ Not optional on a storage appliance, and their absence is why the
    # first pool format failed: base NixOS ships util-linux's sfdisk/fdisk but
    # NO gptfdisk, NO parted and NO partprobe. A host whose entire job is four
    # removable disks must be able to partition one without a rebuild — a drive
    # replacement should not require a deploy before you can prepare the new
    # disk. Discovered the hard way 2026-09-11.
    gptfdisk
    parted
    # e2fsprogs supplies mkfs.ext4/tune2fs/e2fsck; base already has them, but
    # resize2fs matters when a replacement drive is larger than the one it
    # succeeds.
    e2fsprogs
  ];
}
