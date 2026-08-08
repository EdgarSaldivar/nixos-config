{
  # Steam itself runs in Wolf's per-session app container. Installing the host
  # client would add a large graphical/FHS closure with no usable login session;
  # retain only its controller/udev hardware database on the host.
  hardware.steam-hardware.enable = true;

  # Wolf uses uinput for ordinary virtual controllers and uhid for DualSense
  # emulation. hardware.uinput supplies the module, static node, and uinput
  # group; load uhid explicitly and add Wolf's upstream virtual-device rules.
  boot.kernelModules = [ "uhid" ];
  hardware.uinput.enable = true;

  services.udev.extraRules = ''
    KERNEL=="uhid", GROUP="uinput", MODE="0660", TAG+="uaccess"

    KERNEL=="hidraw*", ATTRS{name}=="Wolf PS5 (virtual) pad", GROUP="root", MODE="0660", ENV{ID_SEAT}="seat9"
    SUBSYSTEMS=="input", ATTRS{name}=="Wolf X-Box One (virtual) pad", GROUP="root", MODE="0660", ENV{ID_SEAT}="seat9"
    SUBSYSTEMS=="input", ATTRS{name}=="Wolf PS5 (virtual) pad", GROUP="root", MODE="0660", ENV{ID_SEAT}="seat9"
    SUBSYSTEMS=="input", ATTRS{name}=="Wolf gamepad (virtual) motion sensors", GROUP="root", MODE="0660", ENV{ID_SEAT}="seat9"
    SUBSYSTEMS=="input", ATTRS{name}=="Wolf Nintendo (virtual) pad", GROUP="root", MODE="0660", ENV{ID_SEAT}="seat9"
  '';
}
