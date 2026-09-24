# Fleet metrics and diagnostics

Two layers: a web dashboard for the whole fleet, and interactive tools on every host.

## Fleet Status (Beszel)

<https://status.saldivar.io>, behind Authentik (admins only). It shows live and
historical CPU, memory, disk, disk I/O, network, temperatures, GPU, and systemd units
for every host that runs an agent. Disk SMART health stays in
[Scrutiny](disk-health.md); the agents deliberately leave SMART off.

| Piece | Where | Source |
|---|---|---|
| Hub (web UI, PocketBase) | minas-tirith, `0.0.0.0:8090`, no firewall opening | `hosts/nixos/minas-tirith/beszel-hub.nix` |
| Polled systems | declared, reconciled on every hub start | `hosts/nixos/minas-tirith/beszel-systems.nix` |
| Agent + tools | every NixOS host with `fleet.metrics.enable` | `modules/nixos/fleet/metrics.nix` |
| Route | `status.saldivar.io` → `monitoring/beszel` Service | `hosts/nixos/minas-tirith/traefik-routes/catalog.nix` |
| Contract | ports, auth gate, host list | `checks/fleet-metrics.nix` |

### Trust model

- **Browser → hub.** The hub logs a request in as the user named by
  `X-authentik-email`. Traefik's ForwardAuth overwrites that header, and 8090 is
  reachable only from the Pod bridge (`cni0`). The NixOS firewall alone does not
  achieve that: tailscaled's `ts-input` chain accepts all of `tailscale0` before
  `nixos-fw` runs, so every minas port is open to the tailnet. A raw-table rule
  drops 8090 unless it arrives on `cni0`. Anyone else who reached the port could
  pick an identity; the flake check requires the rule.
  The first hub start creates the user `teremaire@gmail.com` (the Authentik admin)
  with a random break-glass password stored in `/var/lib/beszel-hub/break-glass-password`.
- **Hub → agent.** The hub connects to each agent over SSH on TCP 45876, on
  `tailscale0` only. The agent accepts exactly one key, `fleet.metrics.hubPublicKey`,
  so agents hold no secret. Agents are never reached over the LAN: minas (a /20)
  and nardol (a /24) disagree about the route between them.

### Adding a host

1. Set `fleet.metrics.enable = true;` in the host's `default.nix`.
2. Add it to `beszel-systems.nix` by its MagicDNS name.
3. Rebuild the host, then minas.

A host logged out of Tailscale shows as down in the hub. That is accurate, not a hub fault.

### Hub key rotation or loss

The hub generates its SSH key on first start and publishes the public half at
`/run/beszel-hub/hub.pub` on minas. If `/var/lib/beszel-hub` is lost, the hub makes
a new pair and every agent rejects it: copy the new `hub.pub` into
`fleet.metrics.hubPublicKey` in `modules/nixos/fleet/metrics.nix` and rebuild the fleet.
Metric history is disposable and is not backed up.

## Interactive tools

On every NixOS host:

- `btop`: the default tool for seeing what a box is doing (CPU per core, memory,
  disk I/O, network, processes). GPU hosts get the NVML-aware build, which adds a
  GPU panel.
- GPU hosts (minas-tirith, nardol): `nvtop` for per-process GPU use and VRAM, and
  `glances` for a single-screen overview including sensors and containers.

dol-amroth has `btop` through nix-darwin.
