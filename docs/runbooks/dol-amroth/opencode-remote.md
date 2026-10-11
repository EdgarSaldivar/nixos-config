# opencode on dol-amroth, from anywhere

`https://opencode.saldivar.io` is the opencode 2.x web app driving coding agents on
dol-amroth. The path is Traefik on minas → Authentik passkey (ForwardAuth) → the Mac's
opencode server over the tailnet. Why each piece is there, and the measurements, are in
[`tailnet-services.yaml`](../../../hosts/nixos/minas-tirith/manifests/tailnet-services.yaml);
[`checks/opencode-edge-contract.nix`](../../../checks/opencode-edge-contract.nix) keeps the gate attached.

Two locks, both required: Authentik decides who reaches the Mac through Traefik, and
opencode's own server password refuses everything else on the tailnet.

## The Mac side

The Mac joins the tailnet as `macmax`. opencode's background server listens on that
address only (not Wi-Fi), on 4096, with its password:

```sh
opencode service set hostname "$(/Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4)"
opencode service set port 4096
opencode service set password "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
opencode service restart
```

Never print `opencode service get password`; rotate it with the `set password` line above.

opencode starts its server only when a command needs it. To have it up after a reboot,
`~/Library/LaunchAgents/io.saldivar.opencode-service.plist` runs
`/opt/homebrew/bin/opencode service start` at login with `AbandonProcessGroup`, so the
server outlives the one-shot job. Remote sessions also need the Mac awake: it does not
sleep on AC power (`pmset -g custom`), and a closed lid still sleeps it.

If `tailscale ip -4` ever changes (a re-registered Mac), update the EndpointSlice address
in `tailnet-services.yaml` and deploy pelargir; until then the route answers 502 after
login.

## Deploy

One PR, two hosts, pelargir first (AGENTS.md):

1. Merge, then switch **pelargir** from `origin/master`. It delivers the `tailnet`
   namespace, the Service and EndpointSlice, the Authentik blueprint and CoreDNS. The new
   namespace can produce one self-healing `ApplyManifestFailed` on the first apply.
2. In Authentik → Customization → Blueprints, confirm "Minas Tirith - definite admin
   ForwardAuth applications" applied successfully and the OpenCode provider is on the
   embedded outpost.
3. Switch **minas** from `origin/master` (the route, the callback host, `/etc/hosts`).
4. Before DNS exists, test through the real ingress:
   ```sh
   curl -sI --resolve opencode.saldivar.io:443:10.0.1.6 https://opencode.saldivar.io/
   ```
   An anonymous request must answer `302` to `auth.saldivar.io`. A `502` means Traefik
   cannot reach the Mac: check `tailscale ping macmax` from minas and the Mac's server.
5. Create the DNS record like the other minas names: a Cloudflare CNAME
   `opencode` → minas' public name, DNS only (grey cloud).
6. From a phone off the home network, open the URL, log in with the passkey, then pair
   opencode (below).
7. In a separate PR, add `opencode.saldivar.io  302  auth.saldivar.io` to the ingress
   baseline and deploy pelargir. Doing it before DNS works pages after ~15 minutes.

## Pair a browser

Run this in your own terminal, not through a tool whose output is logged: the link is a
one-time login (5-minute expiry).

```sh
opencode pair --url https://opencode.saldivar.io
```

Open it on the phone after the passkey login. `opencode pair --remote` (OpenTunnel)
is a separate path and not used here.

## Rollback

Remove `opencode` from `authentikRollout.protectedRoutes` and deploy minas: the route
falls back to `basic-auth@file` (hand-maintained on minas), never to open. To take it
down entirely, set `enabled = false` on the route and deploy minas, or stop opencode's
server on the Mac (`opencode service stop`).
