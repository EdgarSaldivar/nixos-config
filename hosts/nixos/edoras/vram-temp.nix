# edoras — GDDR6X memory-junction temperature of the RTX 3090.
#
# NVML does not report it on GeForce cards, and it is the 3090's weak point:
# the memory runs hottest while `nvidia-smi` shows only the core. GA102 exposes
# it in an undocumented register, BAR0 + 0xE2A8, whose low 12 bits / 32 are
# degrees C. This is the method of the open-source `gddr6` tool, also used by the
# `jjziets/gddr6-metrics-exporter` container on terracompute's hosts.
#
# ⛔ It needs `iomem=relaxed`. This kernel is built with CONFIG_IO_STRICT_DEVMEM,
# so /dev/mem refuses MMIO a driver has claimed, even for root: verified
# 2026-11-15, the read failed with "Operation not permitted". The parameter lets
# root map such device registers. Root can already load kernel modules, so on a
# single-purpose host this widens little; it does not reopen RAM, which
# CONFIG_STRICT_DEVMEM still protects.
#
# The read is one 32-bit load through busybox `devmem` (an mmap and a single
# volatile access), not `dd`, which may read MMIO in smaller pieces.
#
# Only GA102's GDDR6X is decoded. The A4000 is GA104 with plain GDDR6; its
# register layout is not known here, so it is skipped rather than guessed.
{ pkgs, ... }:
let
  vramTemp = pkgs.writeShellApplication {
    name = "vram-temp";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      if [ "$(id -u)" -ne 0 ]; then
        echo "vram-temp: needs root (it reads /dev/mem)" >&2
        exit 1
      fi
      found=0
      for dev in /sys/bus/pci/devices/*; do
        [ "$(cat "$dev/vendor")" = 0x10de ] || continue
        case "$(cat "$dev/device")" in
          0x2204) name="RTX 3090" ;;
          *) continue ;;
        esac
        found=1
        bar0="$(head -n1 "$dev/resource" | cut -d' ' -f1)"
        addr="$(printf '0x%x' $((bar0 + 0xE2A8)))"
        raw="$(${pkgs.busybox}/bin/devmem "$addr" 32)"
        echo "$(basename "$dev") $name vram_junction_c=$(((raw & 0xfff) / 32))"
      done
      if [ "$found" -eq 0 ]; then
        echo "vram-temp: no supported GPU (GA102 / RTX 3090) found" >&2
        exit 1
      fi
    '';
  };
in
{
  boot.kernelParams = [ "iomem=relaxed" ];
  environment.systemPackages = [ vramTemp ];
}
