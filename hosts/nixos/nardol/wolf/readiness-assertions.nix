{
  config,
  lib,
  pkgs,
  docker,
  nvidiaAllocatorHostPath,
  nvidiaEglVendorFile,
  nvidiaSmi,
  nvrtcLib,
  renderNode,
  validateWolfConfig,
  wolfConfigData,
  wolfConfigImageLines,
  wolfConfigPolicy,
}:
{
  # The generated OCI service otherwise knows only about Docker/network-online.
  # Refuse to start it before encrypted state and GPU/input prerequisites exist.
  systemd.services.docker-wolf = {
    requires = [
      "srv.mount"
      "nardol-gaming-readiness.service"
    ];
    after = [
      "srv.mount"
      "nardol-gaming-readiness.service"
    ];
    serviceConfig.ExecStartPre = [ (lib.getExe wolfConfigPolicy) ];
  };

  # ⛔ THE READINESS CHECK LATCHES, AND A RESUMED GPU MUST RE-PROVE ITSELF.
  #
  # nardol-gaming-readiness is Type=oneshot with RemainAfterExit=true, which is
  # correct for boot ordering — docker-wolf Requires= it, so it must stay
  # "active" for the dependency to hold. The consequence is that it validates
  # ONCE, at boot, and then reports active forever.
  #
  # That is exactly wrong across a suspend. Every assertion it makes is about
  # state that a sleep transition can invalidate: the render node's driver
  # binding, nvidia_drm modeset, the CDI specification, whether nvidia-smi can
  # still talk to the card. A resume that brings the GPU back broken leaves this
  # unit reporting active from its boot-time run, Wolf still answering on its
  # port, and the host unable to encode a frame. Nothing would report a fault.
  #
  # This re-runs it after every resume. The After=/WantedBy= pairing on
  # suspend.target is the same idiom NixOS itself uses for post-resume work in
  # config/power-management.nix (its post-resume.service), so the ordering
  # semantics are the platform's rather than invented here.
  #
  # A restart, not a start: the unit is RemainAfterExit and therefore already
  # "active", so `systemctl start` would be a no-op and prove nothing.
  systemd.services.nardol-gaming-readiness-resume = {
    description = "Re-verify Nardol's GPU readiness after resume";
    after = [
      "suspend.target"
      "hibernate.target"
      "hybrid-sleep.target"
      "suspend-then-hibernate.target"
    ];
    wantedBy = [
      "suspend.target"
      "hibernate.target"
      "hybrid-sleep.target"
      "suspend-then-hibernate.target"
    ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${pkgs.systemd}/bin/systemctl restart nardol-gaming-readiness.service";
    };
  };

  systemd.services.nardol-gaming-readiness = {
    description = "Verify Nardol's headless NVIDIA and Wolf prerequisites";
    wantedBy = [ "multi-user.target" ];
    requires = [
      "docker.service"
      "srv.mount"
    ];
    after = [
      "docker.service"
      "nvidia-container-toolkit-cdi-generator.service"
      "nvidia-persistenced.service"
      "srv.mount"
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      test -c /dev/uinput
      test -r ${nvidiaAllocatorHostPath}
      test -c /dev/uhid
      test -c ${renderNode}
      test -r ${nvidiaEglVendorFile}
      test -r ${nvrtcLib}/lib/libnvrtc.so
      test "$(${pkgs.coreutils}/bin/cat /sys/module/nvidia_drm/parameters/modeset)" = Y
      test "$(${pkgs.coreutils}/bin/basename "$(${pkgs.coreutils}/bin/readlink -f /sys/class/drm/renderD128/device/driver)")" = nvidia
      test -s /var/run/cdi/nvidia-container-toolkit.json
      ${docker} info --format '{{json .Runtimes}}' | ${pkgs.gnugrep}/bin/grep -q '"nvidia"'
      ${nvidiaSmi} --query-gpu=name,driver_version --format=csv,noheader
    '';
  };

  assertions = [
    {
      assertion =
        wolfConfigImageLines != [ ]
        && lib.all (
          line:
          builtins.match "[[:space:]]*image[[:space:]]*=[[:space:]]*[\"'][^@\"']+@sha256:[0-9a-f]{64}[\"'][[:space:]]*" line
          != null
        ) wolfConfigImageLines;
      message = "nardol: every Wolf template child image must have a registry digest.";
    }
    {
      assertion = config.fileSystems ? "/srv";
      message = "nardol: Wolf state requires the encrypted /srv filesystem.";
    }
    {
      assertion = validateWolfConfig wolfConfigData;
      message = "nardol: every Wolf profile and whole app identity must match the reviewed two-player policy.";
    }
    {
      assertion = config.hardware.nvidia-container-toolkit.enable;
      message = "nardol: Wolf requires the NVIDIA container toolkit.";
    }
    {
      assertion = config.hardware.nvidia.modesetting.enable && config.hardware.nvidia.open;
      message = "nardol: Wolf requires NVIDIA DRM modesetting and the selected open kernel modules.";
    }
  ];
}
