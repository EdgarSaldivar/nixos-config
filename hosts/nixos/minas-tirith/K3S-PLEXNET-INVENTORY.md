# plex-net group — captured inventory (2026-08-07)

Ground truth for the 12 services on the `plex-net` docker network, captured live from
`docker inspect` plus the resource sampler. Per **D14** this network is one atomic
migration unit: its members reference each other by BARE container name, which resolves
only through docker's embedded DNS, so a partial cutover breaks resolution in both
directions at once.

**Verified 2026-08-07:** a Kubernetes namespace restores exactly this property — a Pod in
`media` resolves `http://prowlarr:9696` verbatim via the `media.svc.cluster.local` search
domain. So configs need NO changes provided Service names match the docker ALIASES.

⚠️ **Two aliases differ from their container names.** Services must exist under BOTH:

| container | compose service | aliases used by configs |
|---|---|---|
| `media-tautulli-1` | `tautulli` | `media-tautulli-1`, **`tautulli`** |
| `overseerr` | `overseer` | `overseerr`, **`overseer`** |

Known consumers: `maintainerr` holds `http://tautulli:8181` and `http://overseer:5055`;
`sonarr` holds `http://prowlarr:9696`.

Contains no credentials — scanned before committing, and this repository is public.

```
### media-tautulli-1
image: lscr.io/linuxserver/tautulli:latest
digest: sha256:f0c3e7228c84ad0539f9d5aa9f56c79ef19f6c8d0ad61199d803d32a7b0e080b
user: 
restart: unless-stopped
stopTimeout: <nil>
privileged: false
capAdd: []
devices: 
deviceRequests: 
networkMode: plex-net
ports: 
mounts: /usr/local/etc/tautulli:/config:rw=true /var/lib/plex/Plex Media Server/Logs:/plex_logs:rw=false 

aliases: media-tautulli-1 tautulli 

env:
  PUID=1000
  PGID=1000
  TZ=America/Los_Angeles
  PS1=$(whoami)@$(hostname):$(pwd)\$ 
  LSIO_FIRST_PARTY=true
  TAUTULLI_DOCKER=True
healthcheck: none

traefik: "traefik.http.routers.tautulli.entrypoints":"https" "traefik.http.routers.tautulli.rule":"Host(`tautulli.saldivar.io`)" "traefik.http.routers.tautulli.tls":"true" "traefik.http.services.tautulli.loadbalancer.server.port":"8181" 
workingset: anon=82M ws=116M

### overseerr
image: ghcr.io/sct/overseerr:latest
digest: sha256:6197516c9d7b58ccf113455e32aafb94df5d91995fe50e8c2cae6ba6c7c7b7de
user: 
restart: unless-stopped
stopTimeout: <nil>
privileged: false
capAdd: []
devices: 
deviceRequests: 
networkMode: plex-net
ports: 5055/tcp -> 0.0.0.0:5055 5055/tcp -> [::]:5055 
mounts: /usr/local/etc/docker-overseer:/app/config:rw=true 

aliases: overseerr overseer 

env:
  PGID=1000
  TZ=America/Los_Angeles
  PUID=1000
healthcheck: none

traefik: "traefik.http.routers.overseer.entrypoints":"https" "traefik.http.routers.overseer.rule":"Host(`requests.saldivar.io`) || Host(`overseer.saldivar.io`)" "traefik.http.routers.overseer.tls":"true" "traefik.http.routers.overseer.tls.certresolver":"dns-cloudflare" "traefik.http.services.overseer.loadbalancer.server.port":"5055" 
workingset: anon=150M ws=187M

### jellyfin
image: linuxserver/jellyfin:10.8.9
digest: sha256:7a6f702b41ad9c05f6474f6d3397d06173f73dc1e9186eee1b9f204056a33aed
user: 
restart: unless-stopped
stopTimeout: <nil>
privileged: false
capAdd: []
devices: /dev/dri 
deviceRequests: cdi:[] 
networkMode: plex-net
ports: 8096/tcp -> 0.0.0.0:8096 8096/tcp -> [::]:8096 8920/tcp -> 0.0.0.0:8920 8920/tcp -> [::]:8920 
mounts: /dev/shm:/data/transcode:rw=true /storage/Media/Movies:/data/Movies:rw=false /storage/Media/Television:/data/Television:rw=false /storage/Media/Anime:/data/Anime:rw=false /usr/local/etc/jellyfin/config:/config:rw=true 

aliases: jellyfin jellyfin 

env:
  VERSION=docker
  NVIDIA_VISIBLE_DEVICES=all
  NVIDIA_DRIVER_CAPABILITIES=all
  LANGUAGE=en_US.UTF-8
  LSIO_FIRST_PARTY=true
healthcheck: none

traefik: "traefik.http.routers.jellyfin.entrypoints":"https" "traefik.http.routers.jellyfin.rule":"Host(`jellyfin.saldivar.io`)" "traefik.http.routers.jellyfin.tls":"true" "traefik.http.routers.jellyfin.tls.certresolver":"dns-cloudflare" "traefik.http.services.jellyfin.loadbalancer.server.port":"8096" 
workingset: anon=736M ws=762M

### plex
image: linuxserver/plex:latest
digest: sha256:d8b3417b9173bab8bb79b167119b6b69a85558f2127e7fa810a045654e3dbe7f
user: 
restart: unless-stopped
stopTimeout: <nil>
privileged: false
capAdd: []
devices: /dev/dri 
deviceRequests: cdi:[] 
networkMode: plex-net
ports: 32400/tcp -> 0.0.0.0:32400 32400/tcp -> [::]:32400 
mounts: /dev/shm:/transcode:rw=true /storage/Media/Movies:/data/Movies:rw=false /storage/Media/Television:/data/Television:rw=false /storage/Media/Anime:/data/Anime:rw=false /home/edgar/docker-services/plex/config:/config:rw=true 

aliases: plex plex 

env:
  DOCKER_MODS=linuxserver/mods=plex-absolute-hama
  NVIDIA_DRIVER_CAPABILITIES=all
  PUID=1000
  PGID=1000
  VERSION=docker
  NVIDIA_VISIBLE_DEVICES=all
  LANGUAGE=en_US.UTF-8
  LSIO_FIRST_PARTY=true
  PLEX_DOWNLOAD=https://downloads.plex.tv/plex-media-server-new
  PLEX_ARCH=amd64
  PLEX_MEDIA_SERVER_APPLICATION_SUPPORT_DIR=/config/Library/Application Support
  PLEX_MEDIA_SERVER_MAX_PLUGIN_PROCS=6
  PLEX_MEDIA_SERVER_USER=<REDACTED — see secrets management, not this file>
  PLEX_MEDIA_SERVER_INFO_VENDOR=Docker
healthcheck: none

traefik: "traefik.http.routers.plex.entrypoints":"https" "traefik.http.routers.plex.rule":"Host(`plex.saldivar.io`)" "traefik.http.routers.plex.tls":"true" "traefik.http.services.plex.loadbalancer.server.port":"32400" 
workingset: anon=226M ws=403M

### prowlarr
image: ghcr.io/linuxserver/prowlarr:develop
digest: sha256:dcb53158072c700ab578e6f89d5b4d6efd784e74496e567f98bfa96cd1db3a31
user: 
restart: unless-stopped
stopTimeout: <nil>
privileged: false
capAdd: []
devices: 
deviceRequests: 
networkMode: plex-net
ports: 
mounts: /usr/local/etc/docker-prowlarr:/config:rw=true 

aliases: prowlarr prowlarr 

env:
  TZ=America/Los_Angeles
  PUID=1000
  PGID=1000
  PS1=$(whoami)@$(hostname):$(pwd)\$ 
  LSIO_FIRST_PARTY=true
  COMPlus_EnableDiagnostics=0
  TMPDIR=/run/prowlarr-temp
healthcheck: none

traefik: "traefik.http.routers.prowlarr.entrypoints":"https" "traefik.http.routers.prowlarr.rule":"Host(`prowlarr.saldivar.io`)" "traefik.http.routers.prowlarr.tls":"true" "traefik.http.services.prowlarr.loadbalancer.server.port":"9696" 
workingset: anon=116M ws=224M

### sonarr
image: linuxserver/sonarr
digest: sha256:24acea2956a0ccb11f103877d9f4f8576600fb34bff34820ed749c2256dab89f
user: 
restart: unless-stopped
stopTimeout: <nil>
privileged: false
capAdd: []
devices: 
deviceRequests: 
networkMode: plex-net
ports: 8989/tcp -> 0.0.0.0:8989 8989/tcp -> [::]:8989 
mounts: /home/edgar/docker-services/sonarr/config:/config:rw=true /storage/Media:/storage/Media:rw=true /storage/Media/Torrents:/data:rw=true 

aliases: sonarr sonarr 

env:
  PUID=1000
  PGID=1000
  TZ=PST
  UMASK_SET=18
  PS1=$(whoami)@$(hostname):$(pwd)\$ 
  LSIO_FIRST_PARTY=true
  SONARR_CHANNEL=v4-stable
  SONARR_BRANCH=main
  COMPlus_EnableDiagnostics=0
  TMPDIR=/run/sonarr-temp
healthcheck: none

traefik: "traefik.http.routers.sonarr.entrypoints":"https" "traefik.http.routers.sonarr.rule":"Host(`sonarr.saldivar.io`)" "traefik.http.routers.sonarr.tls":"true" "traefik.http.routers.sonarr.tls.certresolver":"dns-cloudflare" "traefik.http.services.sonarr.loadbalancer.server.port":"8989" 
workingset: anon=182M ws=194M

### radarr
image: linuxserver/radarr
digest: sha256:a45b5ab0f850f39edb4cc9c95bbd967b52ddc3d4574a4dfb45561177db6c88f4
user: 
restart: unless-stopped
stopTimeout: <nil>
privileged: false
capAdd: []
devices: 
deviceRequests: 
networkMode: plex-net
ports: 7878/tcp -> 0.0.0.0:7878 7878/tcp -> [::]:7878 
mounts: /home/edgar/docker-services/radarr/config:/config:rw=true /storage/Media:/storage/Media:rw=true /storage/Media/Torrents/completed:/data/completed:rw=true 

aliases: radarr radarr 

env:
  TZ=PS
  PUID=1000
  PGID=1000
  PS1=$(whoami)@$(hostname):$(pwd)\$ 
  LSIO_FIRST_PARTY=true
  COMPlus_EnableDiagnostics=0
  TMPDIR=/run/radarr-temp
healthcheck: none

traefik: "traefik.http.routers.radarr.entrypoints":"https" "traefik.http.routers.radarr.rule":"Host(`radarr.saldivar.io`)" "traefik.http.routers.radarr.tls":"true" "traefik.http.routers.radarr.tls.certresolver":"dns-cloudflare" "traefik.http.services.radarr.loadbalancer.server.port":"7878" 
workingset: anon=88M ws=187M

### lidarr
image: lscr.io/linuxserver/lidarr:latest
digest: sha256:bfec0ec2dc351fa5928379d785b08be395886f109393b9040ed7973bd1008060
user: 
restart: unless-stopped
stopTimeout: <nil>
privileged: false
capAdd: []
devices: 
deviceRequests: 
networkMode: plex-net
ports: 8686/tcp -> 0.0.0.0:8686 8686/tcp -> [::]:8686 
mounts: /etc/lidarr/data:/config:rw=true /etc/lidarr/music:/music:rw=true /storage/Media/Music:/downloads:rw=true 

aliases: lidarr lidarr 

env:
  TZ=America/Los_Angeles
  PUID=1000
  PGID=1000
  PS1=$(whoami)@$(hostname):$(pwd)\$ 
  LSIO_FIRST_PARTY=true
  COMPlus_EnableDiagnostics=0
  TMPDIR=/run/lidarr-temp
healthcheck: none

traefik: "traefik.http.routers.lidarr.entrypoints":"https" "traefik.http.routers.lidarr.rule":"Host(`lidarr.saldivar.io`)" "traefik.http.routers.lidarr.tls":"true" "traefik.http.routers.lidarr.tls.certresolver":"dns-cloudflare" "traefik.http.services.lidarr.loadbalancer.server.port":"8686" 
workingset: anon=73M ws=165M

### animearr
image: linuxserver/sonarr
digest: sha256:24acea2956a0ccb11f103877d9f4f8576600fb34bff34820ed749c2256dab89f
user: 
restart: unless-stopped
stopTimeout: <nil>
privileged: false
capAdd: []
devices: 
deviceRequests: 
networkMode: plex-net
ports: 8989/tcp -> 0.0.0.0:9292 8989/tcp -> [::]:9292 
mounts: /home/edgar/docker-services/animearr/config:/config:rw=true /storage/Media:/storage/Media:rw=true /storage/Media/Torrents:/data:rw=true 

aliases: animearr animearr 

env:
  PUID=1000
  GUID=1000
  TZ=PST
  UMASK_SET=18
  PS1=$(whoami)@$(hostname):$(pwd)\$ 
  LSIO_FIRST_PARTY=true
  SONARR_CHANNEL=v4-stable
  SONARR_BRANCH=main
  COMPlus_EnableDiagnostics=0
  TMPDIR=/run/sonarr-temp
healthcheck: none

traefik: "traefik.http.routers.animearr.entrypoints":"https" "traefik.http.routers.animearr.rule":"Host(`anime.saldivar.io`)" "traefik.http.routers.animearr.tls":"true" "traefik.http.routers.animearr.tls.certresolver":"dns-cloudflare" "traefik.http.services.animearr.loadbalancer.server.port":"8989" 
workingset: anon=172M ws=184M

### deluge-books
image: ghcr.io/binhex/arch-delugevpn:2.1.1-8-03
digest: sha256:737ef923e400bf6e00595ce6c8fd419002985617e5304a15e66808b1c893b2de
user: 
restart: unless-stopped
stopTimeout: <nil>
privileged: true
capAdd: []
devices: 
deviceRequests: 
networkMode: plex-net
ports: 8112/tcp -> 0.0.0.0:9812 8112/tcp -> [::]:9812 8118/tcp -> 0.0.0.0:9818 8118/tcp -> [::]:9818 48846/tcp -> 0.0.0.0:48846 48846/tcp -> [::]:48846 48946/tcp -> 0.0.0.0:48946 48946/tcp -> [::]:48946 58846/tcp -> 0.0.0.0:59846 58846/tcp -> [::]:59846 58946/tcp -> 0.0.0.0:59946 58946/tcp -> [::]:59946 
mounts: /usr/local/etc/deluge-books:/config:rw=true /storage/Media/Torrents:/data:rw=true /etc/localtime:/etc/localtime:rw=false 

aliases: deluge-books deluge-books 

env:
  VPN_PROV=pia
  PGID=1000
  VPN_CLIENT=openvpn
  VPN_USER=<REDACTED — see secrets management, not this file>
  DEBUG=false
  ENABLE_PRIVOXY=yes
  VPN_ENABLED=yes
  PUID=1000
  LAN_NETWORK=10.0.0.0/24
  VPN_PASS=<REDACTED — see secrets management, not this file>
  NAME_SERVERS=209.222.18.222,84.200.69.80,37.235.1.174,1.1.1.1,209.222.18.218,37.235.1.177,84.200.70.40,1.0.0.1
  STRICT_PORT_FORWARD=yes
healthcheck: [CMD-SHELL curl -sL --fail http://localhost:8112 && curl -sL --fail https://google.com || exit 1] interval=0s start=0s

traefik: "traefik.http.routers.deluge-books.entrypoints":"https" "traefik.http.routers.deluge-books.rule":"Host(`btbooks.saldivar.io`)" "traefik.http.routers.deluge-books.tls.certresolver":"dns-cloudflare" "traefik.http.services.deluge-books.loadbalancer.server.port":"9812" 
workingset: anon=27M ws=51M

### maintainerr
image: ghcr.io/maintainerr/maintainerr:latest
digest: sha256:811a580fdf479e8582d3c97047b1aa8930fc5523f63143498020864ad6a7cd80
user: 1000:1000
restart: unless-stopped
stopTimeout: <nil>
privileged: false
capAdd: []
devices: 
deviceRequests: 
networkMode: plex-net
ports: 
mounts: /usr/local/etc/maintainerr:/opt/data:rw=true 

aliases: maintainerr maintainerr 

env:
  TZ=America/Los_Angeles
  NODE_ENV=production
  DEBUG=false
  UI_PORT=6246
  UI_HOSTNAME=0.0.0.0
  GIT_SHA=41fb882edd6cc68e463bac3202d2555e27654dbf
  DATA_DIR=/opt/data
  VERSION_TAG=latest
  BASE_PATH=
  UV_USE_IO_URING=0
healthcheck: [CMD /opt/app/healthcheck.sh] interval=30s start=40s

traefik: "traefik.http.routers.maintainerr.entrypoints":"https" "traefik.http.routers.maintainerr.middlewares":"basic-auth@file" "traefik.http.routers.maintainerr.rule":"Host(`maintainerr.saldivar.io`)" "traefik.http.routers.maintainerr.tls":"true" "traefik.http.services.maintainerr.loadbalancer.server.port":"6246" 
workingset: anon=126M ws=151M

### wrapperr
image: aunefyren/wrapperr:latest
digest: sha256:2b2021392ae9b1a6c54a98330bdfe62ec0348d4dcba21a71ddf6b46b8b525653
user: 
restart: unless-stopped
stopTimeout: <nil>
privileged: false
capAdd: []
devices: 
deviceRequests: 
networkMode: plex-net
ports: 8282/tcp -> 0.0.0.0:8682 8282/tcp -> [::]:8682 
mounts: /etc/wrapper:/app/config:rw=true 

aliases: wrapperr wrapperr 

env:
  PUID=1000
  PGID=1000
healthcheck: none

traefik: "traefik.http.routers.wrapperr.entrypoints":"https" "traefik.http.routers.wrapperr.rule":"Host(`stats.saldivar.io`) || Host(`wrapperr.saldivar.io`)" "traefik.http.routers.wrapperr.tls":"true" "traefik.http.routers.wrapperr.tls.certresolver":"dns-cloudflare" "traefik.http.services.wrapperr.loadbalancer.server.port":"8282" 
workingset: anon=3M ws=13M

```
