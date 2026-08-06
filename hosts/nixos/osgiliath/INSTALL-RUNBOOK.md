# Osgiliath migration runbook

> **STATUS: CONFIGURATION ONLY. NO DEPLOYMENT HAS OCCURRED.** This repository
> change does not stop Docker, install NixOS, change DNS, reconcile Kubernetes,
> or touch either disk. Every mutating phase below requires explicit future
> permission for that phase. Without it, stop after the read-only preflight.

The migration replaces the NVMe root and cold-restores Frigate, Home Assistant,
Mosquitto, and the local HTTPS edge as k3s workloads pinned to Osgiliath. The
preserved Sabrent ext4 partition and its 331 GiB of recordings are never a disko
target and must never be formatted. The workloads cannot leave init until a
human creates `/var/lib/osgiliath/migration-ready` after all restore gates pass.

## 0. Permission and hard stops

Record the change window, the exact permission granting cutover, the operator,
and the rollback owner. Permission to edit configuration is **not** permission to
run any command in sections 3–9.

Hard stops:

- `secrets/osgiliath.yaml` still contains any decrypted `PLACEHOLDER` value.
- The current SSH ed25519 host key is not backed up and its public half does not
  derive to `age1wfyz5x5xh7gj0ndk07746z905wzmuc20906udqq6c3lr6jg2npjs4fysnx`.
- The router's DHCP reservation for Ethernet MAC `84:47:09:6a:b2:5d` is not
  confirmed to remain `192.168.117.228`. Both restored applications use that
  address for MQTT, and Home Assistant already trusts it as its reverse proxy.
- The external SSD cannot be positively identified by device, ext4 filesystem,
  label, mount, and existing recording content.
- More than one disko disk is configured, or its resolved device is not NVMe.
- The Sabrent SSD is still physically connected when disko runs.
- Any required preservation tool is unavailable. The 2026-08-05 inventory found
  `getfacl` and `smartctl` missing; installing Ubuntu packages `acl` and
  `smartmontools` is a separate, explicitly authorized preparation action.
- A cold state copy or either Home Assistant or Frigate SQLite integrity check
  fails.

## 1. Read-only preflight (safe before cutover permission)

Run on the current Osgiliath without stopping anything:

```sh
date -Is
uname -a
missing=0
for tool in rsync getfacl python3 sha256sum findmnt lsblk smartctl ssh-keygen; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; missing=1; }
done
test "$missing" -eq 0
ip -4 -o address show
ip -4 route
findmnt -no SOURCE,FSTYPE,LABEL,UUID,OPTIONS /mnt/frigate-usb
lsblk -e7 -o NAME,PATH,SIZE,MODEL,SERIAL,TRAN,FSTYPE,LABEL,UUID,MOUNTPOINTS
df -h / /mnt/frigate-usb
sudo smartctl -a "$(findmnt -no SOURCE /mnt/frigate-usb)"

for source in \
  /home/edgar/docker/frigate/config \
  /home/edgar/docker/frigate/addon_configs \
  /home/edgar/docker/homeassistant/config \
  /home/edgar/docker/mosquitto/config \
  /home/edgar/docker/mosquitto/data \
  /home/edgar/docker/mosquitto/log; do
  sudo test -d "$source" || { echo "missing: $source" >&2; exit 1; }
done

sudo docker ps --no-trunc
curl --fail --silent http://127.0.0.1:5000/api/config |
  python3 -c 'import json,sys; c=json.load(sys.stdin)["cameras"]; print("camera count:",len(c)); raise SystemExit(len(c) != 4)'
curl --fail --silent --output /dev/null http://127.0.0.1:8123/
dig +short homeassistant.osgiliath.saldivar.io A
```

Record the external partition source, UUID, ext4 label, parent device, model and
serial in the change ticket. Confirm `/mnt/frigate-usb/frigate` is the existing
recordings tree; do not copy, move, relabel, repartition, or format it.

Confirm in the router—not just from the current DHCP lease—that Ethernet MAC
`84:47:09:6a:b2:5d` is reserved as `192.168.117.228`. The NixOS configuration
keeps DHCP and the same physical MAC, so the reservation preserves the endpoint
without hard-coding the site's gateway, prefix, or DNS servers into the host.

Review the complete configuration from the same commit that a future human will
install. In particular, confirm the controller has imported `wifi.nix`,
`secrets.nix`, `k3s.nix`, and `workload-storage.nix` and that the host evaluates:

```sh
nix eval .#nixosConfigurations.osgiliath.config.networking.hostName
nix eval .#nixosConfigurations.osgiliath.config.system.build.toplevel.drvPath
nix flake check --no-build
```

## 2. Required secret replacement gate

The committed sops document intentionally contains only encrypted
`PLACEHOLDER` values. Replace **all four** interactively before any install:

```sh
secret_tmp="$(mktemp -d)"
chmod 0700 "$secret_tmp"
nix run nixpkgs#ssh-to-age -- \
  -private-key -i /path/to/admin/id_ed25519 -o "$secret_tmp/admin.age"
SOPS_AGE_KEY_FILE="$secret_tmp/admin.age" sops secrets/osgiliath.yaml
```

Set:

- `edgar_password_hash` to a complete crypt password hash, not a password;
- `tailscale_auth_key` to a valid one-time or reusable tailnet auth key;
- `k3s_agent_token` to Pelargir's agent token;
- `wifi_psk_raw` to the 64-hex raw PSK for SSID `Penthouse`.

Validate without printing values. Keep shell tracing disabled:

```sh
set +x
SOPS_AGE_KEY_FILE="$secret_tmp/admin.age" \
  sops exec-env secrets/osgiliath.yaml \
  'python3 -c '\''import os,re; names=("edgar_password_hash","tailscale_auth_key","k3s_agent_token","wifi_psk_raw"); assert all(os.environ.get(n) not in (None,"","PLACEHOLDER") for n in names); assert re.fullmatch(r"[0-9A-Fa-f]{64}",os.environ["wifi_psk_raw"])'\'''
sops filestatus secrets/osgiliath.yaml
shred -u "$secret_tmp/admin.age"
rmdir "$secret_tmp"
```

Do not continue unless that command succeeds. Never paste decrypted output into
the ticket or shell history.

## 3. Cutover: cold shutdown and atomic preservation

This is the first service-affecting phase. Run it only during the explicitly
authorized cutover window. Do not stop Docker during rehearsal or preflight.

```sh
sudo docker ps --no-trunc | sudo tee /root/osgiliath-pre-cutover-containers.txt >/dev/null
sudo systemctl stop docker.service docker.socket
sudo systemctl is-active --quiet docker.service && exit 1 || true
pgrep -a -f 'frigate|home-assistant|mosquitto' && exit 1 || true
```

Create the snapshot entirely on the mounted external ext4 filesystem. `mv` is
atomic because the incomplete and final paths are on that same filesystem:

```sh
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
snapshot_root=/mnt/frigate-usb/osgiliath-migration
incomplete="$snapshot_root/$stamp.incomplete"
final="$snapshot_root/$stamp"

sudo install -d -m 0700 \
  "$snapshot_root" \
  "$incomplete/ssh" \
  "$incomplete/metadata" \
  "$incomplete/state/frigate/config" \
  "$incomplete/state/frigate/addon_configs" \
  "$incomplete/state/homeassistant/config" \
  "$incomplete/state/mosquitto/config" \
  "$incomplete/state/mosquitto/data" \
  "$incomplete/state/mosquitto/log"
sudo rsync -aHAX --numeric-ids /home/edgar/docker/frigate/config/ "$incomplete/state/frigate/config/"
sudo rsync -aHAX --numeric-ids /home/edgar/docker/frigate/addon_configs/ "$incomplete/state/frigate/addon_configs/"
sudo rsync -aHAX --numeric-ids /home/edgar/docker/homeassistant/config/ "$incomplete/state/homeassistant/config/"
sudo rsync -aHAX --numeric-ids /home/edgar/docker/mosquitto/config/ "$incomplete/state/mosquitto/config/"
sudo rsync -aHAX --numeric-ids /home/edgar/docker/mosquitto/data/ "$incomplete/state/mosquitto/data/"
sudo rsync -aHAX --numeric-ids /home/edgar/docker/mosquitto/log/ "$incomplete/state/mosquitto/log/"
sudo getfacl -R -p /home/edgar/docker/frigate /home/edgar/docker/homeassistant /home/edgar/docker/mosquitto |
  sudo tee "$incomplete/metadata/source-acls.txt" >/dev/null
```

Use Python's SQLite backup API on both stopped application databases and fail on
any integrity result other than `ok`:

```sh
sudo python3 - \
  "$incomplete/state/frigate/config/frigate.db.backup" \
  "$incomplete/state/homeassistant/config/home-assistant_v2.db.backup" <<'PY'
import sqlite3
import sys

databases = (
    ("Frigate", "/home/edgar/docker/frigate/config/frigate.db", sys.argv[1]),
    (
        "Home Assistant",
        "/home/edgar/docker/homeassistant/config/home-assistant_v2.db",
        sys.argv[2],
    ),
)

for name, source, destination in databases:
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
        result = src.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise SystemExit(f"{name} source SQLite integrity failure: {result}")
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
            copied = dst.execute("PRAGMA integrity_check").fetchone()[0]
            if copied != "ok":
                raise SystemExit(f"{name} backup SQLite integrity failure: {copied}")
PY
```

Preserve the stable SSH identity without displaying private material:

```sh
sudo install -m 0600 /etc/ssh/ssh_host_ed25519_key "$incomplete/ssh/ssh_host_ed25519_key"
sudo install -m 0644 /etc/ssh/ssh_host_ed25519_key.pub "$incomplete/ssh/ssh_host_ed25519_key.pub"
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub |
  sudo tee "$incomplete/metadata/ssh-host-fingerprint.txt" >/dev/null
sudo sh -c 'cd "$1" && find . -xdev -type f ! -path ./metadata/SHA256SUMS -exec sha256sum {} +' \
  _ "$incomplete" |
  sudo tee "$incomplete/metadata/SHA256SUMS" >/dev/null
sync
sudo mv "$incomplete" "$final"
sudo test -d "$final" && sudo test ! -e "$incomplete"
echo "Record this snapshot path without its contents: $final"
```

From a trusted operator workstation, independently verify the public host key's
age recipient without reading or transferring the private key:

```sh
ssh-keyscan -t ed25519 osgiliath 2>/dev/null |
  nix run nixpkgs#ssh-to-age -- |
  grep -Fx age1wfyz5x5xh7gj0ndk07746z905wzmuc20906udqq6c3lr6jg2npjs4fysnx
```

The completed snapshot contains Osgiliath's SSH private host key. Treat the
external SSD as credential-bearing media. After the rollback window closes,
move the key into the approved encrypted recovery store and remove it from the
migration snapshot; SSD deletion is not guaranteed to be forensic erasure.

Never include `/mnt/frigate-usb/frigate` in an rsync `--delete` target. The
recordings remain in place and are not seed data.

## 4. Installer boot and mechanical disko proof

Boot a NixOS installer. Connect the Sabrent SSD read-only only long enough to
copy the selected snapshot's host keys into installer tmpfs, then unmount and
**physically disconnect it**:

```sh
sudo mkdir -p /mnt-preserved /run/osgiliath-hostkeys
sudo mount -o ro /dev/disk/by-uuid/RECORDED-EXTERNAL-UUID /mnt-preserved
selected=/mnt-preserved/osgiliath-migration/SELECTED-SNAPSHOT
sudo sh -c 'cd "$1" && sha256sum -c metadata/SHA256SUMS' _ "$selected"
sudo install -m 0600 "$selected/ssh/ssh_host_ed25519_key" \
  /run/osgiliath-hostkeys/ssh_host_ed25519_key
sudo install -m 0644 "$selected/ssh/ssh_host_ed25519_key.pub" \
  /run/osgiliath-hostkeys/ssh_host_ed25519_key.pub
sudo umount /mnt-preserved
```

Physically unplug the Sabrent device. Record a photograph or a second operator's
signed observation. Then obtain the configured disko target mechanically:

```sh
cd /path/to/reviewed/nixos-config
nix eval --json .#nixosConfigurations.osgiliath.config.disko.devices.disk |
  jq -r 'to_entries[].value.device' | tee /run/osgiliath-disko-targets
test "$(wc -l < /run/osgiliath-disko-targets)" -eq 1
configured_target="$(cat /run/osgiliath-disko-targets)"
case "$configured_target" in /dev/disk/by-id/nvme-*) ;; *) exit 1 ;; esac
resolved_target="$(readlink -f "$configured_target")"
lsblk -dn -o TYPE,TRAN "$resolved_target" | grep -Eq '^disk[[:space:]]+nvme$'
findmnt /mnt/frigate-usb && exit 1 || true
lsblk -e7 -o NAME,PATH,SIZE,MODEL,SERIAL,TRAN,FSTYPE,LABEL,UUID,MOUNTPOINTS
```

The operator and witness must agree that the sole configured target is the
intended NVMe and the recorded Sabrent model/serial is absent. A path prefix or
visual check alone is insufficient; all commands above must pass. Do not proceed
with any ambiguity.

## 5. NixOS installation (future human commands)

These commands destroy and recreate only the mechanically proven NVMe target.
They are shown for a future authorized human and have **not** been run:

```sh
cd /path/to/reviewed/nixos-config
disko_rev="$(jq -r '.nodes.disko.locked.rev' flake.lock)"
test -n "$disko_rev" && test "$disko_rev" != null
sudo nix run "github:nix-community/disko/$disko_rev" -- \
  --mode destroy,format,mount --flake .#osgiliath
findmnt /mnt

sudo install -d -m 0755 /mnt/etc/ssh
sudo install -m 0600 /run/osgiliath-hostkeys/ssh_host_ed25519_key \
  /mnt/etc/ssh/ssh_host_ed25519_key
sudo install -m 0644 /run/osgiliath-hostkeys/ssh_host_ed25519_key.pub \
  /mnt/etc/ssh/ssh_host_ed25519_key.pub
sudo ssh-keygen -lf /mnt/etc/ssh/ssh_host_ed25519_key.pub
sudo nixos-install --flake .#osgiliath --no-root-passwd
```

Only after disko has exited successfully may the external SSD be reconnected.
Do not run disko again after reconnecting it. Verify its recorded UUID, label and
ext4 type without formatting or relabeling, then reboot with it connected so the
foundation mount and `frigate-storage.target` can verify it.

## 6. First boot and deterministic restore

At the local console, before creating the marker:

```sh
sudo test ! -e /var/lib/osgiliath/migration-ready
sudo systemctl status --no-pager frigate-storage.target
findmnt -no SOURCE,FSTYPE,LABEL,UUID,OPTIONS /mnt/frigate-usb
ip -4 -o address show | grep -Eq '[[:space:]]192\.168\.117\.228/'
sudo systemctl start osgiliath-secrets-gate.service
sudo systemctl status --no-pager osgiliath-secrets-gate.service
sudo systemctl status --no-pager k3s.service
```

Select the completed snapshot explicitly; never use `latest` or a wildcard:

```sh
snapshot=/mnt/frigate-usb/osgiliath-migration/SELECTED-SNAPSHOT/state
sudo test -d "$snapshot"
sudo rsync -aHAX --numeric-ids --delete "$snapshot/frigate/config/" /var/lib/osgiliath/frigate/config/
sudo rsync -aHAX --numeric-ids --delete "$snapshot/frigate/addon_configs/" /var/lib/osgiliath/frigate/addon_configs/
sudo rsync -aHAX --numeric-ids --delete "$snapshot/homeassistant/config/" /var/lib/osgiliath/homeassistant/config/
sudo rsync -aHAX --numeric-ids --delete "$snapshot/mosquitto/config/" /var/lib/osgiliath/mosquitto/config/
sudo rsync -aHAX --numeric-ids --delete "$snapshot/mosquitto/data/" /var/lib/osgiliath/mosquitto/data/
sudo rsync -aHAX --numeric-ids --delete "$snapshot/mosquitto/log/" /var/lib/osgiliath/mosquitto/log/
sync
```

Validate restored state and ownership before releasing workloads:

```sh
sudo test -d /mnt/frigate-usb/frigate
sudo test -c /dev/dri/renderD128
sudo test -d /dev/bus/usb
nix shell nixpkgs#yq-go -c yq -e '.cameras | length == 4' /var/lib/osgiliath/frigate/config/config.yml

sudo python3 - <<'PY'
import sqlite3

databases = (
    ("Frigate", "/var/lib/osgiliath/frigate/config/frigate.db"),
    (
        "Home Assistant",
        "/var/lib/osgiliath/homeassistant/config/home-assistant_v2.db",
    ),
)

for name, path in databases:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        result = db.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise SystemExit(f"{name} restored SQLite integrity failure: {result}")
PY

sudo grep -Eq '^[[:space:]]*listener[[:space:]]+1883([[:space:]]|$)' /var/lib/osgiliath/mosquitto/config/mosquitto.conf
sudo grep -Eq '^[[:space:]]*listener[[:space:]]+9001([[:space:]]|$)' /var/lib/osgiliath/mosquitto/config/mosquitto.conf
sudo grep -Eq '^[[:space:]]*allow_anonymous[[:space:]]+true([[:space:]]|$)' /var/lib/osgiliath/mosquitto/config/mosquitto.conf
sudo -u '#1883' test -r /var/lib/osgiliath/mosquitto/config/mosquitto.conf
sudo -u '#1883' test -w /var/lib/osgiliath/mosquitto/data
sudo -u '#1883' test -w /var/lib/osgiliath/mosquitto/log
sudo find /var/lib/osgiliath -xdev -maxdepth 3 -printf '%u:%g %m %p\n'
```

If either restored primary SQLite database fails, keep the marker absent.
Preserve the failed DB and its WAL/SHM files for analysis, copy the corresponding
`frigate.db.backup` or `home-assistant_v2.db.backup` to a new temporary file,
integrity-check that file, and atomically rename it to the primary filename only
with the rollback owner's approval. Never start either application against a DB
that did not return `ok`.

Do not blanket-`chown` restored trees. `rsync --numeric-ids` preserves the cold
source ownership; investigate and correct only a demonstrated mismatch.

## 7. Release marker and manifest reconciliation

This is a second explicit deployment gate. Obtain permission to activate the
reviewed Osgiliath configuration and the sole Pelargir `manifests.nix` change.
The controller must import the four new host modules first. On each host, use the
normal reviewed deployment path; do not improvise a production command.

Before release, the namespace workloads should be waiting in their identical
`migration-ready` init container. Only after every section 6 check succeeds:

```sh
sudo install -o root -g root -m 000 /dev/null /var/lib/osgiliath/migration-ready
sudo test -f /var/lib/osgiliath/migration-ready
```

No Nix module or tmpfiles rule creates or seeds this marker.

## 8. Rollout and application validation

Run cluster checks from Pelargir's local administrative context:

```sh
sudo k3s kubectl get nodes -o wide
sudo k3s kubectl get node osgiliath \
  -o jsonpath='{.spec.taints}' | grep -F 'osgiliath.saldivar.io/workloads'
sudo k3s kubectl get pods -n kube-system \
  -l svccontroller.k3s.cattle.io/svcname=traefik -o wide
sudo k3s kubectl get all,certificate -n osgiliath
sudo k3s kubectl wait --for=condition=Available --timeout=20m \
  deployment/frigate deployment/home-assistant deployment/mosquitto deployment/osgiliath-edge -n osgiliath
sudo k3s kubectl get pods -n osgiliath -o wide
sudo k3s kubectl get certificate/osgiliath-home-assistant -n osgiliath -o yaml
sudo k3s kubectl get events -n osgiliath --sort-by=.lastTimestamp
```

Every workload pod must show `NODE=osgiliath`, and no Pelargir ServiceLB pod may
show Osgiliath as its node; the dedicated taint prevents its 80/443 hostPorts
from colliding with the local edge. Then validate behavior, not merely pod state:

```sh
curl --fail --silent http://192.168.117.228:8123/ >/dev/null
curl --fail --silent http://192.168.117.228:5000/api/config |
  python3 -c 'import json,sys; c=json.load(sys.stdin)["cameras"]; print(sorted(c)); raise SystemExit(len(c) != 4)'
mosquitto_sub -h 192.168.117.228 -p 1883 -t osgiliath/migration-check -C 1 &
subscriber=$!
mosquitto_pub -h 192.168.117.228 -p 1883 -t osgiliath/migration-check -m ok
wait "$subscriber"
nc -z -w 5 192.168.117.228 9001

dig +short homeassistant.osgiliath.saldivar.io A
openssl s_client -connect OSGILIATH-PUBLIC-IP:443 \
  -servername homeassistant.osgiliath.saldivar.io </dev/null 2>/dev/null |
  openssl x509 -noout -subject -issuer -dates -ext subjectAltName
curl --fail --resolve homeassistant.osgiliath.saldivar.io:443:OSGILIATH-PUBLIC-IP \
  https://homeassistant.osgiliath.saldivar.io/ >/dev/null
```

In Home Assistant, verify login, restored history, secrets-backed integrations,
Bluetooth devices and automations. In Frigate, verify all four live views,
detection through Coral USB, AMD hardware acceleration, RTSP, WebRTC, recording
playback from pre-migration dates, and new writes on the existing external tree.
Verify MQTT 1883 and WebSocket 9001 with real clients. Confirm the public DNS A
record remains Osgiliath's public IP and the router forwards 80/443 to Osgiliath;
do not edit Pelargir ingress resources or HelmChartConfig.

## 9. Rollback

If validation fails, stop the new workloads before changing files. From
Pelargir's local administrative context:

```sh
sudo k3s kubectl scale deployment/frigate deployment/home-assistant \
  deployment/mosquitto deployment/osgiliath-edge --replicas=0 -n osgiliath
```

Then remove the marker on Osgiliath so an accidental scale-up remains blocked:

```sh
sudo rm -f /var/lib/osgiliath/migration-ready
```

For an application-only rollback, restore the selected immutable cold snapshot
again with the section 6 rsync commands, correct the reviewed manifest/config,
and repeat all gates before recreating the marker. For a full legacy rollback,
boot approved rescue media, restore the snapshot directories to their original
`/home/edgar/docker/...` paths with numeric ownership, re-run the SQLite integrity
check, and only then restart the former Docker compositions. The root NVMe has
been reformatted, so a full rollback requires reinstalling the former OS; do not
pretend it is an instant boot-selection rollback.

At no point may rollback partition, relabel, format, or `rsync --delete` the
Sabrent recordings tree. That ext4 filesystem is the preserved source of truth.
