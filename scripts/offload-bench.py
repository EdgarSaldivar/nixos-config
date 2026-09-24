#!/usr/bin/env python3
"""Sweep ik_llama.cpp server configs on nardol and measure prefill/decode.

Companion to scripts/inference-ab.py, which answers "does this deployment
work". This one answers "which flags are worth setting", by running one
throwaway container per config against identical prompts.

Runs from the Mac: containers over ssh, measurements over HTTP. The server's
own /completion timings are the source of truth, so prefill and decode stay
separated -- a single tok/s number folds a 40x cache effect into a 2x engine
difference.

⛔ READ THE NOISE FLOOR BEFORE BELIEVING A RESULT. Measured on nardol
2026-09-17 by running one IDENTICAL config four times, interleaved with
another: decode spanned 26.8-28.4 tok/s, i.e. ±6%. A single run cannot
resolve anything smaller, and a flag that looked like +5.5% (-mqkv) came back
a tie at n=4. Sweep first to find candidates, then repeat the survivors
interleaved before writing any of them into a config file.

⛔ A CONFIG THAT STARTS IS NOT A CONFIG THAT WORKS. Recurrent-state
checkpoints and speculative draft reservations grow with OCCUPANCY, not with
the configured -c, so the failure mode is a server that loads cleanly, serves
short prompts forever, and dies mid-request on the one long turn the context
was raised for. Drive --depths to 92-98% of the ceiling under test; that is
how the 27B's inherited 202752 was caught.

Each config gets a fresh container: reusing one measures the previous
config's page cache.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOST = "10.0.0.118"
PORT = 8001
NAME = "ikbench"
MODEL = "/models/flash-next/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf"

# ⛔ EVERY PROMPT MUST BE UNIQUE, AND REPEATED FILLER SILENTLY INFLATES DECODE.
# A first cut of this script repeated one code snippet, and the live 27B decoded
# it at 169 tok/s against the 115 tok/s scripts/inference-ab.py measures on real
# work -- the MTP drafter accepts nearly every token of text it has already
# seen. hosts/nixos/nardol/inference.nix records the same trap costing a 404
# tok/s reading. Varied identifiers per request keep the number honest, and a
# per-request seed also defeats the server's prompt cache without relying on
# cache_prompt alone.
_WORDS = (
    "handler payload context retry attempt status queue worker shard token budget "
    "cursor batch region tenant invoice ledger schema migration rollback checksum "
    "latency throughput backoff jitter quorum replica snapshot manifest artifact"
).split()


def _rng(seed: int):
    state = seed * 6364136223846793005 + 1442695040888963407
    while True:
        state = (state * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
        yield state >> 33


def ssh(cmd: str, timeout: int = 120) -> str:
    out = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=8", "nardol", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return out.stdout.strip()


def prompt_of(tokens: int, seed: int) -> str:
    """Roughly `tokens` tokens of code-shaped text, unique per seed."""
    r = _rng(seed)
    out = [f"# corpus {seed}\n"]
    n = 0
    while n < tokens:
        w = _WORDS[next(r) % len(_WORDS)]
        k = next(r) % 1000
        out.append(f"def {w}_{k}(shard_{k}, budget={k}):\n")
        out.append(f"    return {w}(shard_{k}) + {k}\n")
        # Calibrated 2026-09-17 against the live server: this block is ~36
        # tokens, not the 18 an eyeball count suggests. Getting it wrong
        # mislabels every depth in the results table by 2x.
        n += 36
    return "".join(out)


def post(path: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        f"http://{HOST}:{PORT}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def wait_healthy(deadline_s: int) -> bool:
    start = time.time()
    while time.time() - start < deadline_s:
        try:
            with urllib.request.urlopen(
                f"http://{HOST}:{PORT}/health", timeout=3
            ) as r:
                if json.loads(r.read()).get("status") == "ok":
                    return True
        except Exception:
            pass
        # A container that died is not going to become healthy.
        if NAME not in ssh(f"sudo -n docker ps --filter name={NAME} --format '{{{{.Names}}}}'"):
            return False
        time.sleep(5)
    return False


def measure(depth: int, n_predict: int, timeout: int, seed: int = 1) -> dict:
    """PP and TG at a given prompt depth. cache_prompt off: every run pays
    prefill, which is the number an agent pays on a cache miss."""
    body = {
        "prompt": prompt_of(depth, seed),
        "n_predict": n_predict,
        "temperature": 0,
        "cache_prompt": False,
        "stream": False,
    }
    r = post("/completion", body, timeout)
    t = r.get("timings", {})
    return {
        "pp_tok_s": round(t.get("prompt_per_second", 0), 1),
        "tg_tok_s": round(t.get("predicted_per_second", 0), 1),
        "prompt_n": t.get("prompt_n"),
        "predicted_n": t.get("predicted_n"),
        "ttft_s": round(t.get("prompt_ms", 0) / 1000, 2),
    }


def vram_mib() -> int:
    v = ssh("nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits")
    try:
        return int(v.splitlines()[0])
    except Exception:
        return -1


def run_config(cfg: dict, depths: list[int], load_deadline: int) -> dict:
    ssh(f"sudo -n docker rm -f {NAME} >/dev/null 2>&1 || true")
    flags = " ".join(cfg["flags"])
    image = cfg.get("image", "ik-llama:local")
    env = " ".join(f"-e {k}={v}" for k, v in cfg.get("env", {}).items())
    cmd = (
        f"sudo -n docker run -d --name {NAME} --gpus=all -p {PORT}:8080 {env} "
        f"-v /srv/inference/gguf:/models:ro {image} "
        f"-m {cfg.get('model', MODEL)} --host 0.0.0.0 --port 8080 --parallel 1 -fa on --jinja {flags}"
    )
    t0 = time.time()
    ssh(cmd, timeout=180)
    if not wait_healthy(load_deadline):
        log = ssh(f"sudo -n docker logs --tail 15 {NAME} 2>&1 || true", timeout=60)
        ssh(f"sudo -n docker rm -f {NAME} >/dev/null 2>&1 || true")
        return {"name": cfg["name"], "error": "did not become healthy", "log": log[-600:]}

    res = {"name": cfg["name"], "load_s": round(time.time() - t0), "vram_mib": vram_mib()}
    try:
        for i, d in enumerate(depths):
            # 64 generated tokens is enough for a stable rate and keeps a slow
            # hybrid config from spending ten minutes per data point. The seed
            # mixes in the config name so no two runs ever share a prompt.
            res[f"d{d}"] = measure(
                d, 64, timeout=1800, seed=abs(hash((cfg["name"], d))) % 10**6 + i
            )
    except Exception as e:  # noqa: BLE001 - report, do not abort the sweep
        res["error"] = f"{type(e).__name__}: {e}"

    res["log_tail"] = ssh(
        f"sudo -n docker logs --tail 3 {NAME} 2>&1 | tr '\\n' ' ' || true", timeout=60
    )[:300]
    ssh(f"sudo -n docker rm -f {NAME} >/dev/null 2>&1 || true")
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", required=True, help="JSON file of configs")
    ap.add_argument("--depths", default="512,4000,32000")
    ap.add_argument("--load-deadline", type=int, default=1500)
    ap.add_argument("--out", default="results.json")
    args = ap.parse_args()

    depths = [int(x) for x in args.depths.split(",")]
    configs = json.load(open(args.configs))
    results = []
    for cfg in configs:
        print(f"\n=== {cfg['name']} ===\n    {' '.join(cfg['flags'])}", flush=True)
        r = run_config(cfg, depths, args.load_deadline)
        results.append(r)
        print("   ", json.dumps({k: v for k, v in r.items() if k != "log_tail"}), flush=True)
        json.dump(results, open(args.out, "w"), indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
