# Fleet disk health

The dashboard is tailnet-only at <http://minas-tirith:9080/web/dashboard>. Minas
runs the native Scrutiny 0.9.2 web/API service and its bundled InfluxDB2. Scrutiny
listens on all addresses so Tailscale can reach it, but the firewall admits TCP
9080 only on `tailscale0`; InfluxDB2 listens only on `127.0.0.1:8086`. There is no
Traefik ingress or privileged container/pod.

Minas-tirith, Pelargir, Osgiliath, and Nardol each run the native collector hourly.
Each has a stable, explicit host ID and submits to `http://minas-tirith:9080` over
Tailscale. Collection uses Scrutiny's `smartctl --scan --json` discovery for
SATA/SAS/USB/NVMe; Minas uses `--scan-open --json` so its Adaptec-attached SATA
disks are classified as SAT at discovery. The timer is persistent, so an overdue
Nardol collection runs after it is powered on; Scrutiny does not page merely
because that intentional on-demand host is offline. Nardol is not a k3s node.

Nardol has no sops auth-key wiring. After its first activation, log in once:

```console
sudo tailscale up
```

Scrutiny invokes a store-resident notification script. The script reads Minas's
existing Healthchecks URL through a systemd credential, writes the latest alert
to `/var/lib/scrutiny/alert.latched` before attempting delivery, and targets the
existing second-line critical channel (falling back to the main URL if no second
line is configured). Minas's aggregate heartbeat remains unhealthy while that
latch exists, even if the original outbound request was missed. Repeated alerts
for an unchanged failing value are disabled; a changed failing value alerts again.
The same heartbeat checks Scrutiny's SQLite and InfluxDB health every five minutes.
After inspecting the disk and recording the response, acknowledge it explicitly:

```console
sudo rm /var/lib/scrutiny/alert.latched
```

Basic verification after an authorized rollout:

```console
tailscale status
systemctl list-timers scrutiny-collector.timer --no-pager
sudo systemctl start scrutiny-collector.service
systemctl status scrutiny-collector.service --no-pager
curl -fsS http://minas-tirith:9080/api/health
```

On Minas, also verify `ss -ltn` shows Scrutiny on `*:9080` and InfluxDB2 only on
`127.0.0.1:8086`. To test the full notification and latch path, POST to
`http://localhost:9080/api/health/notify`; this deliberately creates a test latch
and fails the critical check, so inspect it and run the acknowledgement command
above afterward.
