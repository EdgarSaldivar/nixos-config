# nixos-config

Nix flake managing a small home fleet: five NixOS hosts and one nix-darwin Mac,
plus the k3s cluster they run.

> **This repository is PUBLIC.** Secrets live in sops-encrypted files
> (`secrets/*.yaml`) and are never committed in plaintext. MAC addresses, RFC1918
> addresses and disk serials *are* committed deliberately — the config cannot match
> hardware without them, and none is usable from outside the LAN.

## Fleet

| Output | Platform | Role |
|---|---|---|
| `minas-tirith` | x86_64-linux | media/storage server, k3s agent, public ingress |
| `nardol` | x86_64-linux | RTX 4090: game streaming (Wolf) **or** LLM inference, never both; LUKS unlock via Tang |
| `osgiliath` | x86_64-linux | k3s agent for Frigate — **declared, not yet deployed** |
| `pelargir` | aarch64-linux | Raspberry Pi 5; k3s **server**, Home Assistant, and the wake gateway |
| `imladris` | aarch64-linux | Raspberry Pi 5; archive storage, SMB, Jellyfin, and speech for Assist |
| `dol-amroth` | aarch64-darwin | Mac; drives remote fleet builds |

dol-amroth drives those builds through an `aarch64-linux` guest VM. Bootstrapping it
is a two-stage procedure with a chicken-and-egg problem — see
[`docs/runbooks/dol-amroth/linux-builder.md`](docs/runbooks/dol-amroth/linux-builder.md).

Deployment status is a property of the machines, not of this checkout. One host is
called out above: `osgiliath`, whose configuration is complete while the host itself
still runs its old OS.

`pelargir` and `imladris` both build through the `nixos-raspberrypi` framework and
take their package set from that framework's nixpkgs pin rather than this flake's.
That is the only combination upstream tests and binary-caches, so the divergence is
deliberate and contained to those two appliances. They differ in boot media —
pelargir from NVMe over the PCIe connector, imladris from microSD so that connector
stays free.

`imladris` exists for fault containment rather than capacity. Attaching its four-bay
USB NVMe enclosure to `pelargir` desensed that host's Zigbee coordinator outright and
auto-activated a foreign Proxmox volume group on the sole k3s control plane; storage
is also the part of this fleet that gets physically handled. See
[`hosts/nixos/imladris/default.nix`](hosts/nixos/imladris/default.nix) for the full
reasoning before consolidating it back.

## The voice assistant spans three hosts

Spoken command in, lights out, no cloud in either direction:

| stage | host | why there |
|---|---|---|
| speech to text, text to speech | `imladris` | always awake — see below |
| Home Assistant, Assist pipeline | `pelargir` | already the home-automation host |
| wake gateway | `pelargir` | nardol cannot host the service that wakes nardol |
| language model | `nardol` | the only GPU |

⛔ **The GPU host sleeps, and that shapes every other placement.** `nardol`
suspends to S3 when idle, and an HTTP connection attempt does not send a magic
packet — so a request to a sleeping host fails rather than waits. Two components
exist solely because of this:

- [`inference-gateway.nix`](hosts/nixos/pelargir/inference-gateway.nix) on
  pelargir wakes the host, waits for the model server to be genuinely ready, and
  forwards the request that triggered the wake **exactly once**. Chat
  completions fire tool calls, and tool calls are side effects; a replay can
  turn "the light did not come on" into "it came on twice".
- [`inference-lease.nix`](hosts/nixos/nardol/inference-lease.nix) on nardol makes
  "the host is awake" a fact a caller can *hold* rather than merely observe.
  Polling cannot close that race, because an inhibitor taken after logind has
  admitted a sleep operation is refused. The lease makes logind itself the
  serialisation point.

Speech runs on the Pi for latency, not capacity: the pipeline is wake word → STT
→ LLM → TTS, so putting STT on the sleeping host would serialise the whole resume
in front of the user. On an always-awake host the Pi transcribes *while* nardol
wakes. See [`voice.nix`](hosts/nixos/imladris/voice.nix).

Inference and gaming cannot coexist — 24 GB holds one or the other, and consumer
cards have no MIG. [`gaming-arbitration.nix`](hosts/nixos/nardol/gaming-arbitration.nix)
makes a running game win, and proves the GPU is actually released before a
session starts rather than assuming that a stopped container means free VRAM.

Home Assistant's own configuration is not declarative and this repo does not
pretend otherwise; see
[`home-assistant-desired.yaml`](hosts/nixos/pelargir/home-assistant-desired.yaml)
for what is expected and `scripts/ha-drift.py` for checking that it still holds.

## Layout

| | |
|---|---|
| `hosts/` | one directory per machine, plus its k3s manifests |
| `modules/` | shared modules — see [`modules/README.md`](modules/README.md) for the placement rule |
| `checks/` | the invariants enforced by `nix flake check` |
| `lib/` | `mkHost.nix`, the host constructor |
| `secrets/` | sops-encrypted; recipients in [`.sops.yaml`](.sops.yaml) |
| `docs/` | architecture, operations, and per-host runbooks |
| `scripts/` | `closure-equiv.sh` and `mutation-test.sh`, the refactor safety nets |
| `experiments/` | applied by hand, never auto-deployed |

## Everyday commands

```sh
nix fmt                          # nixfmt-rfc-style
nix flake check                  # the 35 invariants — see below
nh os switch                     # on a NixOS host
nh darwin switch                 # on dol-amroth
```

⛔ **Read [`AGENTS.md`](AGENTS.md) before deploying.** It records the deploy rules
that have caused real outages here — that `nixos-rebuild` cannot run from the Mac,
that manifest changes require rebuilding **pelargir first**, that
`nixos-rebuild test` on pelargir is not a dry run, and that `--rollback` does not
work on this fleet at all.

## Checks

`nix flake check` enforces 35 invariants, most encoding a mistake actually
made here. They live one per file in [`checks/`](checks/), and run natively on
both `x86_64-linux` and `aarch64-darwin`.

⚠️ Run it **without** `--no-build`. Most are evaluation-only, but
`wolf-reconciler` builds a Python environment and runs 57 tests,
`ingress-acceptance` runs 7 offline characterization tests, and
`minas-shell-fixtures` runs the backup and heartbeat bats suites — `--no-build`
skips all three.

The most important is `minas-tirith-disko-targets`. disko destroys exactly the
disks it is handed, and nine of that host's ten drives are live ZFS pool members
holding ~98 TB with no off-site copy. That list is asserted mechanically rather
than re-read by eye before each install.

Two safety nets back structural changes here:

- [`scripts/closure-equiv.sh`](scripts/closure-equiv.sh) pins
  `system.configurationRevision` so a host-closure hash difference means a **real**
  difference rather than just a new commit. A refactor claimed to be a no-op must
  leave all five hashes unchanged.
- [`scripts/mutation-test.sh`](scripts/mutation-test.sh) deliberately violates fourteen
  invariants and asserts each check **throws**. A check reduced to a tautology
  keeps its name and passes `nix flake check`; only a negative test catches that.

CI runs the suite natively on both `x86_64-linux` and `aarch64-darwin` and fails if
the two ever expose different check names — a contract that runs on one platform
only is a contract nobody runs.

## Documentation

- [`docs/architecture/`](docs/architecture/) — how the cluster and ingress fit
  together, and why
- [`docs/operations/`](docs/operations/) — fleet-wide operational surfaces
- [`docs/runbooks/`](docs/runbooks/) — procedures, per machine
- [`ROADMAP.md`](ROADMAP.md) — open work only

The convention, because this repository has drifted before: **source owns facts,
runbooks own actions, and runtime status belongs in monitoring — not in Markdown.**
A runbook says what is *configured*, never what is *running*.

Migration history — plans, reviews, handoffs and ledgers from the 2026 Docker→k3s
move — is in git history behind the `pre-doc-cleanup-2026-08` tag. It was removed
from `HEAD` deliberately: a finished plan sitting beside a live procedure is a
hazard, because a reader at 2am cannot tell which is which.

## Secrets

sops + age. Host keys derive from each machine's SSH ed25519 host key
(`ssh-to-age`), so nothing extra needs provisioning onto a box. The admin identity
derives from `~/.ssh/id_ed25519`, **not** a standalone age key:

```sh
ssh-to-age -private-key -i ~/.ssh/id_ed25519 -o /tmp/age.key
SOPS_AGE_KEY_FILE=/tmp/age.key sops secrets/minas-tirith.yaml
shred -u /tmp/age.key
```

Recipients live in [`.sops.yaml`](.sops.yaml); after adding one, run
`sops updatekeys <file>`.

Rotating a *value* reaches the cluster automatically — `restartUnits` keys off the
decrypted value, and `secret-applier-contract` fails the build if a secret the
applier reads ever loses that wiring. That check exists because a rotated GHCR
credential was once stranded for two hours while every other signal reported
success.
