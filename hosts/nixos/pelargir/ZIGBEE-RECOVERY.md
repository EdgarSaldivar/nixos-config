# Zigbee recovery and Home Assistant restore

## Facts that must not drift

- The intact coordinator uses PAN ID `0x355f`, channel `11`, and nine devices.
- Recovery dumps live only on the Mac at
  `~/Development/pi/zigbee-recovery-2026-08-03/`.
- The coordinator is intact. Never flash it, factory-reset it, or restore a dump
  **into** it. The backup is seed input for Zigbee2MQTT, not programmer input.
- `permit_join` remains false except for a supervised, time-bounded window.

Record and compare hashes before and after every transfer. The repository cannot
record the values because the controller-owned recovery directory is outside
this worktree; paste the resulting digest lines into the private maintenance log:

```bash
set -euo pipefail
cd ~/Development/pi/zigbee-recovery-2026-08-03/
shasum -a 256 coordinator_backup.json
find . -maxdepth 1 -type f -print0 \
  | sort -z \
  | xargs -0 shasum -a 256
```

## Seed Zigbee2MQTT once

From the Mac, send only the coordinator backup to pelargir, then create the
deploy-time Secret. It is absent from git by design.

```bash
set -euo pipefail
scp ~/Development/pi/zigbee-recovery-2026-08-03/coordinator_backup.json \
  pelargir:/tmp/coordinator_backup.json
ssh pelargir \
  'sudo k3s kubectl -n home create secret generic zigbee2mqtt-coordinator --from-file=coordinator_backup.json=/tmp/coordinator_backup.json'
ssh pelargir 'rm /tmp/coordinator_backup.json'
```

The init container copies both this file and the sops-rendered
`configuration.yaml` into the writable PVC only when `.seeded` is absent.
Zigbee2MQTT subsequently rewrites its live backup there; never mount a Secret at
the live path. To redo a seed, scale the deployment down, inspect and archive the
PVC, remove `.seeded` deliberately, then scale up. Do not casually delete it.

```bash
set -euo pipefail
sudo k3s kubectl -n home rollout status deployment/zigbee2mqtt --timeout=300s
sudo k3s kubectl -n home logs deployment/zigbee2mqtt --all-containers=true
```

Routers normally re-announce. Wake sleeping battery devices with their ordinary
button; do not reset or re-pair them unless recovery evidence says the network
identity failed.

## Temporary join window

Keep `permit_join: false` as the declarative default. If one device truly needs
joining, use Zigbee2MQTT's UI to open the shortest practical window, supervise
the log and physical device, then close it immediately and verify it is false.
Never make a temporary window a manifest edit.

## Rename all nine devices before HA reconciliation

For every recovered IEEE address, find its previous friendly name in the old
recovery material and restore it exactly. Check off all nine privately:

1. IEEE address matched to the old record.
2. Old friendly name restored exactly, including case and punctuation.
3. MQTT publishes under the old base topic.
4. Home Assistant entity/device continuity is confirmed.

This order matters: MQTT topic equals friendly name, so a new friendly name
looks like a new HA entity even when the radio device and coordinator are intact.

## Seed Home Assistant and perform the upgrade ladder

Home Assistant's PVC is initially empty. On pelargir, find its local-path
directory after the claim binds and stop the deployment. From the Mac, copy the
snapshot tree, preserving dotfiles:

```bash
set -euo pipefail
ssh pelargir 'sudo k3s kubectl -n home scale deployment/home-assistant --replicas=0'
rsync -a --delete \
  ~/Development/pi/homeassistant/config/ \
  pelargir:/tmp/homeassistant-config/
```

On pelargir, resolve the bound PV path rather than guessing it, then install the
snapshot and remove the staging copy:

```bash
set -euo pipefail
pvc_uid="$(sudo k3s kubectl -n home get pvc home-assistant-config -o jsonpath='{.metadata.uid}')"
pvc_path="$(sudo find /var/lib/rancher/k3s/storage -maxdepth 1 -type d -name "pvc-${pvc_uid}_home_home-assistant-config" -print -quit)"
test -n "$pvc_path"
sudo rsync -a --delete /tmp/homeassistant-config/ "$pvc_path/"
sudo chown -R root:root "$pvc_path"
rm -rf /tmp/homeassistant-config
```

Before starting HA, edit the restored `configuration.yaml` to merge this block
with any existing `http:` settings (do not create a second `http:` key):

```yaml
http:
  use_x_forwarded_for: true
  trusted_proxies:
    - 10.42.0.0/16
    - 127.0.0.1
```

The manifest intentionally pins 2024.11.1 for the restore boot. Start it, wait
for every DB migration, inspect logs, stop it, and take a PVC/restic checkpoint.
Then advance through the last release of each intervening major year/month
recommended by Home Assistant release notes—never directly to current 2026.x.
At each intermediate image digest:

1. pre-pull the immutable digest;
2. scale down and checkpoint the PVC/database;
3. update the manifest digest and scale up;
4. wait for migrations to finish and inspect logs/UI;
5. test Zigbee/MQTT entities before continuing.

Only after the final 2026.x checkpoint is healthy may the 2024.11.1 restore
digest be replaced permanently. A failed migration rolls back to the preceding
PVC checkpoint and image together; rolling back only the image is unsafe after a
schema migration.
