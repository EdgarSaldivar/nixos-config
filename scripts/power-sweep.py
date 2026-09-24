#!/usr/bin/env python3
"""Tokens per joule across GPU power limits, on the live endpoint.

Decode on this workload is bound by VRAM bandwidth, not by shader throughput,
so a 4090 held at 450 W spends most of that budget on clocks it cannot use.
Prefill IS compute-bound, so the two have to be measured separately or the
answer is an average of two different curves.

No restart is needed: `nvidia-smi -pl` takes effect immediately, so the served
model stays up throughout and every configuration sees the identical model,
identical prompts and identical cache state.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import urllib.request

HOST = "10.0.0.118"


def ssh(cmd: str, timeout: int = 120) -> str:
    return subprocess.run(["ssh", "-o", "ConnectTimeout=8", "nardol", cmd],
                          capture_output=True, text=True, timeout=timeout).stdout.strip()


def set_pl(watts: int) -> str:
    return ssh(f"sudo -n nvidia-smi -pl {watts} 2>&1 | tail -1")


def sampler_start() -> None:
    ssh("sudo -n rm -f /tmp/pw.log; nohup sudo -n nvidia-smi "
        "--query-gpu=power.draw --format=csv,noheader,nounits -lms 200 "
        "> /tmp/pw.log 2>/dev/null & echo started")


def sampler_window() -> list[float]:
    raw = ssh("cat /tmp/pw.log 2>/dev/null | tail -2000")
    out = []
    for line in raw.splitlines():
        try:
            out.append(float(line.strip()))
        except ValueError:
            pass
    return out


def sampler_stop() -> None:
    ssh("sudo -n pkill -f 'query-gpu=power.draw' >/dev/null 2>&1; true")


def post(path: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(f"http://{HOST}:8000{path}",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def prompt_of(tokens: int, seed: int) -> str:
    state = seed * 6364136223846793005 + 1442695040888963407
    words = "handler payload context retry shard ledger cursor backoff quorum replica".split()
    out, n = [], 0
    while n < tokens:
        state = (state * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
        w = words[(state >> 33) % len(words)]
        k = (state >> 17) % 997
        out.append(f"def {w}_{k}(shard_{k}):\n    return {w}(shard_{k}) + {k}\n")
        n += 30
    return "".join(out)


def measure(depth: int, n_predict: int, seed: int) -> dict:
    """One request; power is averaged over the samples taken during it."""
    before = len(sampler_window())
    t0 = time.time()
    r = post("/completion", {"prompt": prompt_of(depth, seed), "n_predict": n_predict,
                             "temperature": 0, "cache_prompt": False, "stream": False}, 1800)
    wall = time.time() - t0
    samples = sampler_window()[before:]
    t = r.get("timings", {})
    # Drop the first few samples: they catch the tail of the previous idle period.
    useful = samples[2:] if len(samples) > 6 else samples
    return {
        "pp_tok_s": round(t.get("prompt_per_second", 0), 1),
        "tg_tok_s": round(t.get("predicted_per_second", 0), 1),
        "wall_s": round(wall, 1),
        "watt_avg": round(statistics.mean(useful), 1) if useful else -1,
        "watt_peak": round(max(useful), 1) if useful else -1,
        "n_samples": len(useful),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limits", default="450,400,350,300,250,200,150")
    ap.add_argument("--depth", type=int, default=4000)
    ap.add_argument("--predict", type=int, default=400)
    ap.add_argument("--out", default="power.json")
    a = ap.parse_args()

    limits = [int(x) for x in a.limits.split(",")]
    rows = []
    sampler_start()
    try:
        for i, w in enumerate(limits):
            set_pl(w)
            time.sleep(3)
            # Warm once at this limit so clocks settle before the measured run.
            measure(512, 64, seed=1)
            # ⛔ ONE SEED FOR EVERY LIMIT. Varying the prompt per limit varies MTP
            # draft acceptance, and that moves decode by ~13% -- which showed up
            # as a "dip" that followed the THIRD position in the sequence rather
            # than any particular wattage. Same prompt everywhere, or the sweep
            # measures prompts instead of power.
            m = measure(a.depth, a.predict, seed=42)
            m["limit_w"] = w
            # tokens per joule, decode phase: tok/s divided by watts
            m["tg_per_watt"] = round(m["tg_tok_s"] / m["watt_avg"], 4) if m["watt_avg"] > 0 else -1
            rows.append(m)
            print(f"  {w:3}W -> TG {m['tg_tok_s']:6.1f} tok/s @ {m['watt_avg']:5.1f}W avg "
                  f"(peak {m['watt_peak']:5.1f})  PP {m['pp_tok_s']:7.1f}  "
                  f"eff {m['tg_per_watt']:.4f} tok/J", flush=True)
    finally:
        sampler_stop()
        set_pl(450)

    json.dump(rows, open(a.out, "w"), indent=2)
    base = next((r for r in rows if r["limit_w"] == max(limits)), rows[0])
    print("\nrelative to default:")
    for r in rows:
        print(f"  {r['limit_w']:3}W  TG {100 * r['tg_tok_s'] / base['tg_tok_s']:5.1f}%  "
              f"PP {100 * r['pp_tok_s'] / base['pp_tok_s']:5.1f}%  "
              f"power {100 * r['watt_avg'] / base['watt_avg']:5.1f}%  "
              f"eff {100 * r['tg_per_watt'] / base['tg_per_watt']:5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
