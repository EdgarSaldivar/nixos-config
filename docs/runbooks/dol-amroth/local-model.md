# The local model on dol-amroth

dol-amroth serves one model itself, for opencode's subagents, so nardol's context
goes to the main agent. The package, its pinned llama.cpp, every server flag and the
measurements behind them are in [`pkgs/amon-din-local.nix`](../../../pkgs/amon-din-local.nix).
Amon Dîn's menu shows it under **This Mac**.

It is a user LaunchAgent (`io.saldivar.llama-server`) running `amon-din-local-serve`
from the nix profile on `http://127.0.0.1:8080/v1`, model id `qwen36-heretic`. It
sleeps after 10 idle minutes, which frees ~19 GiB, and the next request wakes it.

## Install or update

The model file is not in the store. Fetch it once (18.7 GB):

```sh
nix shell nixpkgs#python3Packages.huggingface-hub -c hf download \
  mradermacher/Qwen3.6-35B-A3B-uncensored-heretic-i1-GGUF \
  Qwen3.6-35B-A3B-uncensored-heretic.i1-IQ4_XS.gguf \
  --local-dir ~/.lmstudio/models/mradermacher/Qwen3.6-35B-A3B-uncensored-heretic-i1-GGUF
```

For vision, fetch the projector into the same directory (585 MiB). It is the base
model's, from ggml-org, not mradermacher's; the package explains why. Without it
the server starts text only.

```sh
nix shell nixpkgs#python3Packages.huggingface-hub -c hf download \
  ggml-org/Qwen3.6-35B-A3B-GGUF mmproj-Qwen3.6-35B-A3B-Q8_0.gguf \
  --local-dir ~/.lmstudio/models/mradermacher/Qwen3.6-35B-A3B-uncensored-heretic-i1-GGUF
shasum -a 256 ~/.lmstudio/models/mradermacher/Qwen3.6-35B-A3B-uncensored-heretic-i1-GGUF/mmproj-Qwen3.6-35B-A3B-Q8_0.gguf
# 904cbf8c8e876220066ab3bf676c7efa40f3da372276fdaf8b01d2fb2a37a51d
```

The rest of Amon Dîn is installed from the local checkout's `master`, so these are
too. Bring `master` up to the merged commit first. The first build compiles
llama.cpp, about 10 minutes:

```sh
F='git+file:///Users/edgar/Development/nixos-config?ref=master'
nix profile install "$F#amon-din-local" "$F#amon-din-local-serve"   # first time
nix profile upgrade --all                                            # afterwards
amon-din-local install     # writes the LaunchAgent and starts it
amon-din-local restart     # after an upgrade, to run the new server
```

`install` overwrites any hand-written plist with the same label.

## Day to day

```sh
amon-din-local status      # off | starting | asleep | awake | busy
amon-din-local wake        # load it now instead of on the first request
amon-din-local stop        # free the memory until the next login
amon-din-local start
amon-din-local log         # ~/Library/Logs/llama-server.log in Console
```

⛔ **Do not poll the server for status with anything but `/health` or `/props`.**
`/slots` reloads the sleeping model. `amon-din-local status` is safe; the contract
check `amon-din-local-contract` enforces it.

## Use a different model

Set `local_model=/absolute/path.gguf` (and optionally `local_ctx=`,
`local_mmproj=` for its projector or `none` for text only, or `local_sleep=` for
the idle timeout in seconds) in
`~/.config/amon-din/config`, then `amon-din-local restart`. Measure it first: the
context ceiling and recall are per-model facts, and opencode's `limit.context`
must stay inside what recalled every needle.

## opencode

The provider lives in `~/.config/opencode/opencode.jsonc`, outside this repository:
provider `mac` (`settings.baseURL` `http://127.0.0.1:8080/v1`, models `heretic` and
`heretic-think`, both with `modelID` `qwen36-heretic` and `capabilities.input`
`["text", "image"]`), and `agent.explore.model` / `agent.general.model` set to
`mac/heretic`. Keep `limit.context` at 112000 so compaction fires before the depth
where recall slipped.

⛔ **Write providers in opencode 2.0's native form, with sampling and
`chat_template_kwargs` under `body`.** 2.0 migrates a 1.x model's `options` to
`settings`, which an OpenAI-compatible provider never sends: on 2026-10-11 the
"thinking" models ran with thinking off and no sampling until they were moved.
After editing, `opencode reload`; the background service keeps its config until then.

The `nardol` provider's `limit.context` follows the model nardol serves: Amon Dîn
rewrites it on every line ending `// amon-din:nardol-context` and reloads opencode
when the serving profile changes (`amon-din-opencode-sync`; contract check
`amon-din-opencode-sync-contract`). Lines without the marker are never touched.

## Running agents from the phone

Remote sessions stop when the Mac sleeps. **This Mac → Keep Mac awake on power**
disables system sleep on AC power only (`pmset -c sleep 0`; battery and lid-close
are unchanged) and remembers the previous value for turning it off.

The phone client is [Remote for OpenCode](https://remoteforopencode.com/): its Mac
companion owns `opencode serve` on localhost and pairs with the iPhone over iCloud,
end-to-end encrypted, so nothing listens on the network. Whatever drives opencode can
run shell commands on this Mac, so never expose `opencode serve` itself beyond
localhost.
