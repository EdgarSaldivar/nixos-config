# Nardol client microphone

Nardol accepts one VBAN microphone stream from the Mac at `10.0.0.17` and
publishes it inside Wolf's shared PulseAudio daemon as
`Nardol-Client-Microphone`. Both existing Steam profiles use that source as
their default microphone; this does not add a game-specific Wolf application.

## Mac setup

Install **VBAN Talkie Cherry** from the Mac App Store and grant it microphone
and local-network access. If the network prompt does not appear, enable the
app under **System Settings → Privacy & Security → Local Network**. Configure
its outgoing stream as follows:

| Setting | Value |
|---|---|
| Audio input | The microphone to use |
| Target IP | `10.0.0.118` |
| UDP port | `6980` |
| Stream name | `NardolMic` |
| Format | Mono, 48 kHz, 16-bit |

Start the outgoing stream before opening voice chat. The app's mute or
push-to-talk control stops speech from reaching Nardol without changing Wolf.

The Nardol service and firewall both accept only source address `10.0.0.17` on
`eth0`. If the Mac's DHCP address changes, update `vbanClientAddress` in
`hosts/nixos/nardol/wolf.nix`, rebuild, and redeploy; do not add port 6980 to
the unrestricted UDP allowlist.

## Verification

After deploying the NixOS configuration and starting Wolf, verify the receiver:

```sh
systemctl status nardol-vban-microphone
sudo journalctl -u nardol-vban-microphone -b
docker exec wolf sh -lc \
  'PULSE_SERVER=unix:/run/wolf/pulse-socket pactl list short sources'
```

The source list must contain `nardol_client_mic`. Start the VBAN stream and
inspect its PulseAudio recording path from the running Steam container:

```sh
steam_container="$(docker ps --filter name=WolfSteam --format '{{.Names}}' | head -1)"
docker exec "$steam_container" sh -lc \
  'printf "PULSE_SOURCE=%s\n" "$PULSE_SOURCE"; pactl get-source-mute nardol_client_mic'
```

`PULSE_SOURCE` must be `nardol_client_mic`. In BIG WALK, leave the input at its
PulseAudio default or select **Nardol-Client-Microphone** if the game presents
a device picker.

VBAN is unauthenticated UDP audio. This configuration intentionally accepts it
only from the Mac on the physical LAN. Do not expose UDP 6980 through the
router; use a private overlay network and a correspondingly scoped firewall
rule before using it away from home.
