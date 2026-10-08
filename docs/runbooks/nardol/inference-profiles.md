# Inference profiles on nardol

A profile is a model plus the engine that serves it: ik-llama, vLLM or mainline
llama.cpp. Profiles are data in
[`lib/inference-profiles.nix`](../../../lib/inference-profiles.nix), and the
measured reasons behind each one are written next to it there. Every profile
runs in the same unit, `docker-ikllama.service`. The unit's launcher reads the
selected profile when it starts, and the name stays `ikllama` whichever engine
is behind it. The launcher, and why the name is frozen, are in
[`inference.nix`](../../../hosts/nixos/nardol/inference.nix).
[`nardol-inference-contract.nix`](../../../checks/nardol-inference-contract.nix)
enforces the profile rules.

## Switch profile

From the Amon Dîn menu on the Mac, or on nardol:

```sh
nardol-model list                    # * marks the selected profile
sudo nardol-model switch <profile>   # restarts the unit; waits up to 900 s for /health
```

The switch restarts the unit even when the new profile uses another engine.
It refuses to restart during a live game. It records the choice instead, and
the new profile starts when gaming ends. vLLM takes about 2.5 minutes to answer
`/health`; ik-llama takes under a minute.

Confirm what the server is running, not what the menu says:

```sh
journalctl -u docker-ikllama -n 50 | grep 'inference: serving profile'
curl -s localhost:8000/health
```

## Model files

The weights live on the host and are not in git.

| engine | location | fetch |
|---|---|---|
| ik-llama, llama-cpp | `/srv/inference/gguf/` (vision files in `vision/`) | `hf download <repo> <file> --local-dir ...` |
| vLLM | Hugging Face cache under `/srv/inference/hub/` | `hf download <repo>` |

The launcher runs vLLM with `HF_HUB_OFFLINE=1`, so a missing checkpoint fails
at start instead of downloading. The `hf` CLI is in the pinned vLLM image:

```sh
sudo docker run --rm -v /srv/inference:/root/.cache/huggingface \
  --entrypoint hf vllm/vllm-openai@sha256:<pinned digest> download <repo>
```

## Patched vLLM image

`qwen3.8-27b-vllm-batch` runs on a local build of the pinned vLLM image with a
two-line patch for quantized token embeddings. The build is in
[`vllm-embedq/Dockerfile`](../../../hosts/nixos/nardol/vllm-embedq/Dockerfile).
The profile pins the build by image ID, so after any rebuild:

```sh
sudo docker build -t vllm-openai:0.30.0-embedq hosts/nixos/nardol/vllm-embedq
sudo docker image inspect vllm-openai:0.30.0-embedq --format '{{.Id}}'
```

Put the printed ID in the profile's `image` field and deploy. If the image is
missing, the switch fails at start. There is no stock-image fallback profile;
to run batch work without the patch, download `RedHatAI/Qwen3.8-27B-INT4` and
add a profile for it (it has no MTP and does about 26 clips/min).

## Sampling and thinking

Every Qwen profile serves Qwen's non-thinking sampling preset unless a request sends
its own values. The server keeps thinking off because Home Assistant cannot
turn it off per request. Coding clients should turn it on per request with
`chat_template_kwargs: {"enable_thinking": true}` and Qwen's thinking preset
(temperature 1.0, top_p 0.95, top_k 20). The reasons and the measurements are
on `ikSampling` in `inference.nix`. GLM-4.6V-Flash takes the sampling from its own
checkpoint config, which already matches its model card.

## Add a profile

1. Download the files as described in [Model files](#model-files).
2. Add an entry to `lib/inference-profiles.nix`. Set only the fields its
   engine uses. `nix flake check` rejects:
   - a field that belongs to another engine,
   - a non-ik profile without `maxModelLen`,
   - a vLLM profile without `model`, or a llama-cpp profile without `ggufFile`.
3. Find the context ceiling by filling the context, not by starting the server.
   A config that starts can still run out of memory at depth, and a vision
   profile can run out on its first multi-image request. Fill to about 97%
   occupancy, then send an image request, before writing a number down.
4. Deploy as AGENTS.md describes, then switch to the profile and confirm the
   journal line above.

## Before you switch nardol

`nixos-rebuild switch` restarts this unit, and gaming is exclusive with it.
Check `systemctl is-active nardol-gaming.target` first (AGENTS.md section 1).
The Dungeon Scriber worker takes the GPU whenever inference is stopped. To run
an inference container by hand, pause the worker first; see
[dungeon-scriber-worker.md](dungeon-scriber-worker.md#pause-or-disable).
