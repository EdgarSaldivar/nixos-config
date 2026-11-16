# edoras — RTX 3090 power cap, 300 W (stock 420 W, max 450 W).
#
# The cap protects the card's GDDR6X, which the core temperature hides.
# Measured 2026-11-15 with ./vram-temp.nix, under gpu-burn -tc (tensor cores,
# unlocked by ./gpu-unlock.nix) with both cards loaded:
#
#   limit   GDDR6X junction                 core   3090 throughput
#   420 W   104 C within 3 minutes (abort)  80 C   83.4 TFLOP/s
#   300 W   86-88 C plateau over 10 min     71 C   67.2 TFLOP/s
#
# 300 W leaves ~16 C to the 104 C abort line and ~22 C to GDDR6X's ~110 C
# throttle, and still beats the card's locked stock rate (66.0 TFLOP/s at
# 420 W before the unlock). Token generation is memory-bound and draws less
# than a tensor burn, so it barely notices the cap. The vram-temp-monitor
# journal is how to tell if a real workload ever runs the memory hotter.
#
# The card is selected by UUID, never by index, because PCI order can change.
# The limit holds while the driver stays loaded, which nvidia-persistenced
# ensures; this unit reapplies it on every boot.
{ config, ... }:
let
  watts = 300;
  rtx3090 = "GPU-fe6604a2-e3a8-7eb0-2716-8fb9d8327cba";
  nvidiaSmi = "${config.hardware.nvidia.package.bin}/bin/nvidia-smi";
in
{
  systemd.services.rtx3090-power-cap = {
    description = "Cap the RTX 3090 at ${toString watts} W";
    wantedBy = [ "multi-user.target" ];
    wants = [ "nvidia-persistenced.service" ];
    after = [ "nvidia-persistenced.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${nvidiaSmi} -i ${rtx3090} -pl ${toString watts}";
    };
  };
}
