# Systems the Beszel hub polls. Its own file so checks/fleet-metrics.nix can
# require that every name is a host that actually runs an agent.
#
# Hosts use MagicDNS names: agents listen for the hub only on tailscale0, never
# the LAN, where minas' /20 and nardol's /24 disagree about the route (the
# 2026-08-10 outage class). A host logged out of Tailscale shows as down, which
# is true. osgiliath joins this list when it is installed.
[
  {
    name = "minas-tirith";
    host = "localhost";
  }
  {
    name = "pelargir";
    host = "pelargir";
  }
  {
    name = "imladris";
    host = "imladris";
  }
  {
    name = "nardol";
    host = "nardol";
  }
]
