# Deterministic host paths for cold-restored application state. The migration
# marker is deliberately absent: only the future cutover runbook may create it
# after all state and ownership checks pass.
{
  # Leaf modes/owners are intentionally "-": tmpfiles creates missing leaves,
  # then rsync's cold restore remains authoritative across later boots.
  systemd.tmpfiles.rules = [
    "d /var/lib/osgiliath 0755 root root -"
    "d /var/lib/osgiliath/frigate 0755 root root -"
    "d /var/lib/osgiliath/frigate/config - - - -"
    "d /var/lib/osgiliath/frigate/addon_configs - - - -"
    "d /var/lib/osgiliath/homeassistant 0755 root root -"
    "d /var/lib/osgiliath/homeassistant/config - - - -"
    "d /var/lib/osgiliath/mosquitto 0755 root root -"
    "d /var/lib/osgiliath/mosquitto/config - - - -"
    "d /var/lib/osgiliath/mosquitto/data - - - -"
    "d /var/lib/osgiliath/mosquitto/log - - - -"
  ];
}
