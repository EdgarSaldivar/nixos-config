# PC1 — pre-change cluster baseline

Captured **2026-08-06**, before P1B (encryption at rest) and before any service migration.
Purpose: any later deviation from this is an **abort condition**. Secret *names* and
counts only — never values; this file is committed to git.

## Nodes and taints
```
NAME           STATUS   VERSION        TAINTS
minas-tirith   Ready    v1.35.6+k3s1   <none>
pelargir       Ready    v1.35.6+k3s1   node-role.kubernetes.io/control-plane
```

## k3s AddOns (packaged components under k3s ownership)
```
NAME                        SOURCE
aggregated-metrics-reader   /var/lib/rancher/k3s/server/manifests/metrics-server/aggregated-metrics-reader.yaml
auth-delegator              /var/lib/rancher/k3s/server/manifests/metrics-server/auth-delegator.yaml
auth-reader                 /var/lib/rancher/k3s/server/manifests/metrics-server/auth-reader.yaml
ccm                         /var/lib/rancher/k3s/server/manifests/ccm.yaml
coredns                     /var/lib/rancher/k3s/server/manifests/coredns.yaml
ddns                        /var/lib/rancher/k3s/server/manifests/ddns.yaml
home-assistant              /var/lib/rancher/k3s/server/manifests/home-assistant.yaml
ingress                     /var/lib/rancher/k3s/server/manifests/ingress.yaml
local-storage               /var/lib/rancher/k3s/server/manifests/local-storage.yaml
metrics-apiservice          /var/lib/rancher/k3s/server/manifests/metrics-server/metrics-apiservice.yaml
metrics-server-deployment   /var/lib/rancher/k3s/server/manifests/metrics-server/metrics-server-deployment.yaml
metrics-server-service      /var/lib/rancher/k3s/server/manifests/metrics-server/metrics-server-service.yaml
mosquitto                   /var/lib/rancher/k3s/server/manifests/mosquitto.yaml
namespace                   /var/lib/rancher/k3s/server/manifests/namespace.yaml
osgiliath-edge              /var/lib/rancher/k3s/server/manifests/osgiliath-edge.yaml
osgiliath-frigate           /var/lib/rancher/k3s/server/manifests/osgiliath-frigate.yaml
osgiliath-home-assistant    /var/lib/rancher/k3s/server/manifests/osgiliath-home-assistant.yaml
osgiliath-mosquitto         /var/lib/rancher/k3s/server/manifests/osgiliath-mosquitto.yaml
osgiliath-namespace         /var/lib/rancher/k3s/server/manifests/osgiliath-namespace.yaml
pelargir-home-secrets       /var/lib/rancher/k3s/server/manifests/pelargir-home-secrets.yaml
resource-reader             /var/lib/rancher/k3s/server/manifests/metrics-server/resource-reader.yaml
rolebindings                /var/lib/rancher/k3s/server/manifests/rolebindings.yaml
runtimes                    /var/lib/rancher/k3s/server/manifests/runtimes.yaml
traefik                     /var/lib/rancher/k3s/server/manifests/traefik.yaml
zigbee2mqtt                 /var/lib/rancher/k3s/server/manifests/zigbee2mqtt.yaml
```

## StorageClasses
```
NAME         PROVISIONER             RECLAIM   DEFAULT
local-path   rancher.io/local-path   Delete    true
```

## Secrets — names and count (NO values)
```
  cert-manager/cert-manager-webhook-ca  Opaque
  cert-manager/cloudflare-api-token  Opaque
  cert-manager/letsencrypt-account-key  Opaque
  cert-manager/pelargir-wildcard-tls  kubernetes.io/tls
  cert-manager/sh.helm.release.v1.cert-manager.v1  helm.sh/release.v1
  home/cloudflare-origin-pull-ca  Opaque
  home/ddns-updater-config  Opaque
  home/mosquitto-auth  Opaque
  home/pelargir-wildcard-tls  kubernetes.io/tls
  home/zigbee2mqtt-config  Opaque
  home/zigbee2mqtt-coordinator  Opaque
  kube-system/chart-values-cert-manager  helmcharts.helm.cattle.io/values
  kube-system/chart-values-reflector  helmcharts.helm.cattle.io/values
  kube-system/chart-values-traefik  helmcharts.helm.cattle.io/values
  kube-system/chart-values-traefik-crd  helmcharts.helm.cattle.io/values
  kube-system/k3s-serving  kubernetes.io/tls
  kube-system/minas-tirith.node-password.k3s  k3s.cattle.io/node-password
  kube-system/pelargir.node-password.k3s  k3s.cattle.io/node-password
  kube-system/sh.helm.release.v1.reflector.v1  helm.sh/release.v1
  kube-system/sh.helm.release.v1.traefik-crd.v1  helm.sh/release.v1
  kube-system/sh.helm.release.v1.traefik.v1  helm.sh/release.v1
  osgiliath/osgiliath-edge-tls  kubernetes.io/tls
TOTAL: 22
```

## Secrets encryption status (P1B changes exactly this)
```
Encryption Status: Disabled, no configuration file found
```

## Ingress routes
```
NS     NAME             HOSTS
home   home-assistant   ha-pelargir.saldivar.io
```

## Workloads — readiness (the four Pending osgiliath pods are EXPECTED)
```
  cert-manager   cert-manager-cainjector-7996d59c89-gsb86 Running    pelargir
  cert-manager   cert-manager-d55696cd6-hhcb6       Running    pelargir
  cert-manager   cert-manager-webhook-6565cc777d-9klld Running    pelargir
  home           ddns-updater-5c7dbb4b65-7bpn6      Running    pelargir
  home           home-assistant-dd88cdd59-sfcd9     Running    pelargir
  home           mosquitto-6f97f7fbdb-fr67w         Running    pelargir
  home           zigbee2mqtt-5ffcf6b545-95gtl       Running    pelargir
  kube-system    coredns-8b64bcf7c-h227x            Running    pelargir
  kube-system    helm-install-cert-manager-9dj2l    Completed  pelargir
  kube-system    helm-install-reflector-lr2pn       Completed  pelargir
  kube-system    helm-install-traefik-crd-r7tmk     Completed  pelargir
  kube-system    helm-install-traefik-kxs9w         Completed  pelargir
  kube-system    local-path-provisioner-5d9d9885bc-7pw5c Running    pelargir
  kube-system    metrics-server-786d997795-zmzn5    Running    pelargir
  kube-system    reflector-666759fcfc-c2cdz         Running    pelargir
  kube-system    svclb-traefik-65e837bf-n9p9t       Running    pelargir
  kube-system    traefik-d98ccfdf6-l7kg2            Running    pelargir
  osgiliath      frigate-685589c8c9-sdqlm           Pending    <none>
  osgiliath      home-assistant-69767f557-bbsr9     Pending    <none>
  osgiliath      mosquitto-75c9d97646-lg8j9         Pending    <none>
  osgiliath      osgiliath-edge-c489b86ff-k4ptq     Pending    <none>
```

## PVCs
```
home   home-assistant-config   Bound   pvc-ea6e0f96-6998-4e26-84a6-50dd977f38c2   20Gi   RWO   local-path   <unset>   41h
home   mosquitto-data          Bound   pvc-756b7181-0c5e-4685-ace2-3de6c659dd70   2Gi    RWO   local-path   <unset>   41h
home   zigbee2mqtt-data        Bound   pvc-81432382-0678-41a3-8fae-8d6d69a4d611   4Gi    RWO   local-path   <unset>   41h
```

## minas-tirith — running docker containers (the migration source of truth)

> Note: `docker` requires **sudo** on minas — `edgar` is not in the docker group. A
> capture without it silently reports zero containers rather than failing, which is how
> the first attempt at this baseline recorded `TOTAL RUNNING: 0` on a host running 32.

```
animearr	Up 3 hours
audiobookshelf	Up 5 hours (healthy)
calibre	Up 6 hours
deluge-books	Up 5 hours (unhealthy)
deluge-vpn	Up 5 hours (healthy)
flaresolverr-books	Up 5 hours
flaresolverr	Up 6 hours
gluetun	Up 5 hours (healthy)
immich-postgres14	Up 5 hours
immich-redis	Up 5 hours
immich	Up 5 hours
jellyfin	Up 4 hours
kavita	Up 5 hours (healthy)
komga	Up 3 hours
lidarr	Up 6 hours
maintainerr	Up 6 hours (healthy)
media-tautulli-1	Up 6 hours
media-tracearr-1	Up 6 hours (healthy)
nextcloud-db	Up 5 hours (healthy)
nextcloud-redis	Up 5 hours (unhealthy)
nextcloud	Up 5 hours
overseerr	Up 6 hours
palworld-server	Up 18 seconds (health: starting)
plex	Up 3 hours
prowlarr	Up 6 hours
qbittorrent-books	Up 5 hours (healthy)
radarr	Up 3 hours
readmeabook	Up 5 hours (healthy)
shelfmark	Up 5 hours (healthy)
sonarr	Up 3 hours
traefik2	Up 6 hours
wrapperr	Up 6 hours

TOTAL RUNNING: 32
```

---

## Deviations already present at baseline — NOT caused by the migration

The point of recording these is that "any deviation from baseline aborts" only works if
the baseline is honest about what is *already* broken. Migrate any of these unchanged and
the resulting failure will look exactly like migration damage.

| container | state at baseline | what it actually is |
|---|---|---|
| `palworld-server` | **611 restarts**, up 18s | genuinely crash-looping, long before any migration |
| `deluge-books` | unhealthy, 0 restarts, up 5h | the *health check* exceeds its own 30s timeout; the app is up |
| `nextcloud-redis` | unhealthy, 0 restarts, up 5h | health check sends `-a <password>` to a redis with **no password configured** — `AUTH failed: called without any password configured`. The check is wrong, not the service |

### ⚠️ Migration trap in the two "unhealthy" rows

Both are *health-check* defects, not service defects, and docker tolerates them: an
unhealthy container keeps running and keeps serving. **Kubernetes does not.** Port these
checks over as readinessProbes and the Pods never become Ready — traffic is never routed,
Deployments never finish rolling out, and the migration appears to have broken two
services that were in fact working the whole time.

So for these two the checks must be **fixed or dropped at migration time**, not
transliterated. `nextcloud-redis` in particular needs its `-a` argument removed (or a
password actually configured) — copying the compose healthcheck verbatim guarantees a
permanently NotReady Pod.

`palworld-server` is the opposite case: it is really broken. Decide before migrating
whether to fix it or leave it behind; a 611-restart service moved onto k8s becomes a
CrashLoopBackOff that will be blamed on the migration.

## ⚠️ Storage: the default StorageClass reclaims by DELETE

`local-path` is the **default** StorageClass and its reclaim policy is **`Delete`**. Any
PVC created without an explicit class inherits it, and then:

- `kubectl delete pvc …` destroys the data, and
- **`kubectl delete namespace …` deletes the PVCs in it, and therefore the data.**

The second is the dangerous one, because deleting and recreating a namespace is a normal
thing to do while iterating on a migration. With 35 services carrying irreplaceable state
(immich photos, nextcloud files, media libraries) this is a live footgun sitting on the
default path.

Most bulk data stays on ZFS and should arrive as hostPath/local volumes rather than
provisioned PVCs, which limits the exposure — but any *new* PVC is exposed by default.
**P1D must add a `Retain` class and make stateful workloads name it explicitly.** Note a
class's `reclaimPolicy` is immutable, so this has to be a second class, not an edit.
