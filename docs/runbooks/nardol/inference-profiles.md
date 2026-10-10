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

`qwen3.8-27b-vllm-batch` and both `qwen3.8-27b-heretic-ara` profiles run on a
local build of the pinned vLLM image with a two-line patch for quantized token
embeddings. The build is in
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

## Try a model from Hugging Face

A tried profile serves any Hugging Face model without a commit. It is one JSON
file in `/var/lib/nardol-inference/profiles.d/`, outside git and outside the
flake check, so treat it as an experiment, not as fleet config.

```sh
sudo nardol-model try <org/repo>            # download, add, switch
sudo nardol-model try <org/repo> --file '*Q5_K_M*' --name my-model --no-switch
sudo nardol-model tweak <name> ctx=65536 kv=q4_0 seqs=2
sudo nardol-model forget <name>             # switch away, delete files and profile
sudo nardol-model promote <name>            # print a lib/inference-profiles.nix entry
```

From the Mac: Amon Dîn → Model → **Try a model from Hugging Face…**, and
**Forget a tried model**. Tried models appear under **Tried** while nardol is up.

- **A GGUF repo** runs on ik_llama (`--engine llama-cpp` for an architecture ik
  lacks). The default pick is Q4_K_M, then Q4_K_S, IQ4_XS, Q5_K_M; every shard of
  a split file comes along, and an mmproj (Q8_0 preferred) if the repo has one.
  Files go to `/srv/inference/gguf/user/<name>/`.
- **A safetensors repo** runs on vLLM, FP8 when the checkpoint is unquantized.
  Repos that need `trust_remote_code` (an `auto_map` in `config.json`) are
  refused. Files go to the shared HF cache; `forget` deletes only blobs no other
  model uses.
- Every tried profile starts at 32k context. Nothing has measured its ceiling;
  raise `ctx` with `tweak` and fill the context before relying on it.
- A vLLM tried profile always prefills in 8192-token chunks, and `tweak` cannot
  change that. Some models run out of memory mid-prefill at that size even
  though they start cleanly. A tried profile can only lower `gpu`; a measured
  profile can set `--max-num-batched-tokens` itself (see the heretic-ara
  profiles).
- **Gated repos:** accept the terms on huggingface.co, then put a *read* token in
  `/var/lib/nardol-inference/hf-token` (root, mode 0600). It is passed through
  temporary 0600 files, never on a command line.

### When a tried model does not come up

`switch` returns to the default and exits 1. The unit also counts failed starts
per tried profile in `/var/lib/nardol-inference/user-failures/`: after two, the
launcher serves the default instead, and `/status` shows `fallback`. A clean
switch resets the count. Fix it with `tweak` (usually a smaller `ctx` or `gpu`),
then switch again. A model too big for 24 GB fails this way; pick a smaller
quant with `--file`.

To keep a tried model, run `promote`, move its GGUFs out of `gguf/user/`, add the
entry, measure it as [Add a profile](#add-a-profile) says, then `forget` it.

## Before you switch nardol

`nixos-rebuild switch` restarts this unit, and gaming is exclusive with it.
Check `systemctl is-active nardol-gaming.target` first (AGENTS.md section 1).
The Dungeon Scriber worker takes the GPU whenever inference is stopped. To run
an inference container by hand, pause the worker first; see
[dungeon-scriber-worker.md](dungeon-scriber-worker.md#pause-or-disable).
