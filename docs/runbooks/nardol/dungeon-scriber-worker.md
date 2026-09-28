# Dungeon Scriber worker on nardol

The worker leases transcription jobs from the Dungeon Scriber API and holds the
4090 while it runs them. It is the lowest-priority GPU tenant: a game always
wins, inference outranks it, and more than 6 GiB of GPU memory held by anything
else makes it yield. Queued jobs wait. The reasoning, and the exact systemd
relationships, are in the header of
[`dungeon-scriber-worker.nix`](../../../hosts/nixos/nardol/dungeon-scriber-worker.nix).
[`nardol-dungeon-scriber-worker-contract.nix`](../../../checks/nardol-dungeon-scriber-worker-contract.nix)
enforces them.

The module is imported but off. Merging it changes nothing on the host.

## Files the owner creates on nardol

All of these live outside this repository. None of them may ever be committed,
because this repository is public. Create them as `edgar`, mode `0600`, in
`/home/edgar/dungeon-scriber-worker/`. That is the default `stateDir`; every path
below is an option.

| file | content |
|---|---|
| `worker-token-minas` | the worker bearer token (already present) |
| `hf-token` | Hugging Face read token (already present); set `hfTokenFile = null` if unused |
| `api.env` | exactly one line: `DS_API_BASE_URL=https://minas-tirith.<tailnet>.ts.net` |
| `ghcr-token` | GitHub token with **only** `read:packages`, when pulling from GHCR |

```sh
install -m 0600 /dev/null ~/dungeon-scriber-worker/api.env
$EDITOR ~/dungeon-scriber-worker/api.env
```

The unit refuses to start, and says why in the journal, if a token file is
missing or empty, or if `api.env` is not a single `https://` origin. It creates
`cache/models` and `home` itself if they are absent.

⚠️ With a `ghcr-token`, the unit runs `docker login ghcr.io` before each start.
Docker then keeps that credential in root's Docker config on nardol. Revoking the
token on GitHub is what actually retires it.

## Enable

1. Choose an image:
   - **GHCR, preferred.** Use the digest CI published, never a tag:
     ```nix
     nardol.dungeonScriberWorker = {
       enable = true;
       image = "ghcr.io/edgarsaldivar/dungeon-scriber-worker@sha256:<digest>";
       registryLogin = {
         username = "edgarsaldivar";
         passwordFile = "/home/edgar/dungeon-scriber-worker/ghcr-token";
       };
     };
     ```
   - **Local fallback.** A tag already built on nardol. Docker never pulls it:
     ```nix
     nardol.dungeonScriberWorker = {
       enable = true;
       image = "dungeon-scriber-worker:<revision tag>";
       localImage = true;
     };
     ```
   Worker settings such as `DS_DIARIZATION_BACKEND` go in `environment`.
   Credentials and the API origin are refused there.
2. Add the block to `hosts/nixos/nardol/default.nix`, then run
   `nix flake check`.
3. Deploy following [AGENTS.md](../../../AGENTS.md). **Check
   `systemctl is-active nardol-gaming.target` first.** Enabling adds units, but
   the switch rule for docker-ikllama applies to every nardol switch.
4. The resume timer starts the worker up to two minutes after boot or within
   `resumeIntervalSec` of the switch, once the gate is clear.

## When the worker runs

The gate
([`dungeon-scriber-worker-gate.nix`](../../../hosts/nixos/nardol/dungeon-scriber-worker-gate.nix))
allows the worker only when all of these hold:

- `nardol-gaming.target` is `inactive`;
- the inference unit (`yieldUnits`) is neither active nor starting;
- Wolf reports no sessions and no lobbies, and it answered;
- other workloads hold at most `thresholdMiB` (6144) MiB of GPU memory, and
  `nvidia-smi` answered.

⚠️ **The gaming target stays active after a session ends.** With idle-suspend
disabled, nothing stops it. Stopping it brings inference back through
`nardol-inference-restore`, and inference outranks the worker. So on a host
that has just gamed, the worker runs only after **both** of these:

```sh
sudo systemctl stop nardol-gaming.target   # ends gaming mode; inference restarts
sudo systemctl stop docker-ikllama          # optional: give the GPU to the worker
```

Stopping inference is the owner's choice. Home Assistant has no model until
`nardol-model serve` or the next restore brings it back.

## Status and logs

```sh
systemctl status docker-dungeon-scriber-worker dungeon-scriber-worker-guard
systemctl list-timers dungeon-scriber-worker-resume.timer
journalctl -u docker-dungeon-scriber-worker -f            # the worker's own log
journalctl -u dungeon-scriber-worker-guard -u dungeon-scriber-worker-resume \
  -u dungeon-scriber-worker-yield --since -1h             # why it stopped or started
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
```

A start refused by the gate shows as `condition failed` with the reason on the
line before, for example `worker must yield: nardol-gaming.target is active`.
That is normal and is not a failure.

## Verify that it yields to a game and resumes

Run this only with the owner present and no session anyone is using.

1. With the worker `active` and `nvidia-smi` listing its process, do what
   amon-din does:
   ```sh
   sudo systemctl start nardol-gaming.target
   ```
   Expect `start` to return within seconds. `dungeon-scriber-worker-yield`
   should have run, the worker should be `inactive`, and `nardol-gpu-handover`
   should log `GPU released`. The session is unaffected.
2. While the target is active, `sudo systemctl start docker-dungeon-scriber-worker`
   must report a skipped condition. The target must remain active.
3. Direct-Moonlight path: stop the target, let the worker come back, then start a
   Moonlight session **without** amon-din. Within `guardIntervalSec` the guard
   logs `Wolf reports 1 sessions; stopping the worker`.
4. Inference path: with the worker running and no session, run
   `sudo systemctl start docker-ikllama`. The guard logs
   `docker-ikllama.service is activating` and stops the worker before the model
   finishes loading. The 6 GiB memory rule is the backstop for workloads the
   gate does not name. The contract tests it against fakes, and it has no live
   step here.
5. Resume: remove every blocker (see [When the worker runs](#when-the-worker-runs)).
   The worker is `active` again within `resumeIntervalSec`.

## What an interruption costs

A stopped worker does not report its job. The job's API lease expires after
`DS_LEASE_SECONDS` (300 s by default), and the next lease retries it from the
start. Every lease consumes one of the job's attempts. A final-transcript job has
five, so a job interrupted five times dead-letters as `LEASE_EXPIRED`, and
someone has to re-enqueue it on the API side. A `nixos-rebuild switch` that
changes the worker unit restarts a running worker, and that interruption costs
an attempt too.

## Pause or disable

- **Pause until the next boot or switch:**
  ```sh
  sudo systemctl stop dungeon-scriber-worker-resume.timer docker-dungeon-scriber-worker
  ```
  Stop the timer first, or it restarts the worker within a minute.
- **Disable:** set `nardol.dungeonScriberWorker.enable = false` (or remove the
  block) and deploy as above. The container stops and its units disappear. The
  model cache, token files, image and Docker credential stay on the host.
  Removing any of them is a separate, explicitly approved deletion.
