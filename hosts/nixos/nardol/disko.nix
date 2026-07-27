# Disk layout for nardol.
#
# SAFETY: this intentionally declares ONLY the Samsung 970 EVO Plus. The
# SN850X (Ubuntu, still the escape hatch) and the P3 Plus (games, Proxmox
# LVs) are deliberately absent, so a stray `disko` run cannot destroy them.
# Add them here only once you have decided to give them up.
#
# Devices are referenced by-id, never /dev/nvmeXn1 — those renumber when PCIe
# enumeration changes, which is exactly what broke networking on this box.
#
# No LUKS: full-disk encryption with an interactive unlock means the machine
# cannot boot unattended. If you want encryption later, put it on a data
# partition with a keyfile and leave root plain.
{
  disko.devices.disk.root = {
    device = "/dev/disk/by-id/nvme-Samsung_SSD_970_EVO_Plus_2TB_S6S2NS0T629836M";
    type = "disk";
    content = {
      type = "gpt";
      partitions = {
        # nardol's own ESP. Ubuntu's ESP lives on the SN850X and is left
        # untouched — the two installs stay independent, selected from the
        # BIOS boot menu.
        ESP = {
          type = "EF00";
          size = "1G";
          content = {
            type = "filesystem";
            format = "vfat";
            mountpoint = "/boot";
            mountOptions = [ "umask=0077" ];
          };
        };

        root = {
          size = "100%";
          content = {
            type = "filesystem";
            format = "ext4";
            mountpoint = "/";
          };
        };
      };
    };
  };
}
