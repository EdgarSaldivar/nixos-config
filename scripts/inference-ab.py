#!/usr/bin/env python3
"""A/B a Qwen3.6-27B deployment across inference runtimes.

⛔ THIS EXISTS BECAUSE THE PUBLISHED BENCHMARK DOES NOT MEASURE WHAT IT CLAIMS.

The widely cited RTX 4090 "6/6 quality pass" harness reports a `tool` category,
but its code never submits an OpenAI `tools` array and never inspects
`message.tool_calls` -- it asks the model to print JSON in ordinary content.
Home Assistant does not work that way, so that result says nothing about whether
HA will function. Its tokens/sec also divides output tokens by TOTAL request
time, folding prefill into decode and understating both.

So this harness insists on three things the other does not:

  1. TOOLS GO THROUGH THE TOOLS API. A pass requires a structured tool_calls
     entry with the right function name and correctly typed arguments. Prose
     that happens to contain braces is a failure.
  2. PREFILL AND DECODE ARE SEPARATED, via streaming. Time to first token is
     prefill; everything after is decode. A single tok/s number conflates a
     40x cache effect with a 2x engine difference.
  3. WARM TURNS ARE MEASURED. Every candidate engine has an open report of
     cache flags being accepted while the prompt is silently reprocessed in
     full -- llama.cpp #25023/#25913, vLLM #45238. Turn 1 cannot detect it;
     only comparing turn 2's TTFT against turn 1's can.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "set_light",
            "description": "Turn a light on or off and optionally set brightness.",
            "parameters": {
                "type": "object",
                "properties": {
                    "area": {"type": "string", "description": "Room name"},
                    "state": {"type": "string", "enum": ["on", "off"]},
                    "brightness": {"type": "integer", "minimum": 0, "maximum": 100},
                },
                "required": ["area", "state"],
            },
        },
    }
]


class Result:
    """One measured request."""

    def __init__(self) -> None:
        self.ttft: float | None = None
        self.total: float = 0.0
        self.out_tokens: int = 0
        self.content: str = ""
        self.tool_calls: list = []
        self.error: str | None = None

    @property
    def decode_tps(self) -> float:
        """Decode rate ALONE. Excludes prefill, which is the point."""
        if self.ttft is None or self.out_tokens < 2:
            return 0.0
        span = self.total - self.ttft
        return (self.out_tokens - 1) / span if span > 0 else 0.0


def chat(base: str, messages: list, *, tools=None, max_tokens=256,
         temperature=0.0, timeout=600, extra=None) -> Result:
    """One streamed chat completion, timed."""
    res = Result()
    body = {
        "model": "default",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if extra:
        body.update(extra)

    req = urllib.request.Request(
        f"{base.rstrip('/')}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    partial_args: dict = {}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    res.out_tokens = chunk["usage"].get("completion_tokens", res.out_tokens)
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    # ⛔ ENGINES SPELL THE REASONING FIELD DIFFERENTLY.
                    # vLLM streams "reasoning"; llama.cpp streams
                    # "reasoning_content". Checking only one makes the other
                    # engine look like it never emitted a token -- measured
                    # 2026-09-13, where this reported 0.0 tok/s and "no first
                    # token" against a llama-server that was in fact working
                    # perfectly. A harness that only speaks to one runtime
                    # cannot A/B two.
                    piece = (
                        delta.get("content")
                        or delta.get("reasoning")
                        or delta.get("reasoning_content")
                        or ""
                    )
                    if piece and res.ttft is None:
                        res.ttft = time.perf_counter() - started
                    res.content += piece or ""
                    # Tool calls stream as fragments and must be reassembled by
                    # index; a single chunk is never a whole call.
                    for tc in delta.get("tool_calls") or []:
                        if res.ttft is None:
                            res.ttft = time.perf_counter() - started
                        slot = partial_args.setdefault(
                            tc.get("index", 0), {"name": "", "arguments": ""}
                        )
                        fn = tc.get("function") or {}
                        slot["name"] += fn.get("name") or ""
                        slot["arguments"] += fn.get("arguments") or ""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        res.error = str(exc)
    res.total = time.perf_counter() - started
    res.tool_calls = [v for _, v in sorted(partial_args.items())]
    if not res.out_tokens:
        res.out_tokens = max(1, len(res.content) // 4)
    return res


def filler(tokens: int) -> str:
    """Deterministic pseudo-code filler, ~4 chars per token."""
    unit = "def helper_{i}(value):\n    return value * {i} + 1\n\n"
    out = []
    i = 0
    while sum(len(x) for x in out) < tokens * 4:
        out.append(unit.format(i=i))
        i += 1
    return "".join(out)


def check_tools(res: Result) -> tuple[bool, str]:
    """A real tool call, or a failure. Prose containing JSON does not count."""
    if res.error:
        return False, f"transport: {res.error}"
    if not res.tool_calls:
        got = res.content.strip()[:60].replace("\n", " ")
        return False, f"no tool_calls (content: {got!r})"
    call = res.tool_calls[0]
    if call["name"] != "set_light":
        return False, f"wrong function {call['name']!r}"
    try:
        args = json.loads(call["arguments"])
    except json.JSONDecodeError as exc:
        return False, f"arguments not valid JSON: {exc} :: {call['arguments'][:60]!r}"
    if "area" not in args or "state" not in args:
        return False, f"missing required keys: {sorted(args)}"
    if args["state"] not in ("on", "off"):
        return False, f"state not in enum: {args['state']!r}"
    if "brightness" in args and not isinstance(args["brightness"], int):
        return False, f"brightness not an integer: {args['brightness']!r}"
    return True, json.dumps(args, sort_keys=True)


def run(base: str, ctx_sizes: list[int], repeats: int) -> int:
    failures = 0

    def report(name: str, ok: bool, detail: str) -> None:
        nonlocal failures
        if not ok:
            failures += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<34} {detail}")

    print(f"\n=== {base} ===")

    # 1. Decode rate, isolated from prefill.
    rates = []
    for _ in range(repeats):
        r = chat(base, [{"role": "user", "content": "Count from 1 to 60, comma separated."}],
                 max_tokens=220)
        if r.error:
            report("decode", False, r.error)
            break
        rates.append(r.decode_tps)
    if rates:
        report("decode (isolated)", True,
               f"{statistics.median(rates):.1f} tok/s median of {len(rates)}")

    # 2. Prefill, from TTFT on a large prompt.
    for n in ctx_sizes:
        prompt = filler(n)
        r = chat(base, [{"role": "user", "content": prompt + "\nReply with OK."}],
                 max_tokens=8)
        if r.error or r.ttft is None:
            report(f"prefill {n//1000}k", False, r.error or "no first token")
            continue
        report(f"prefill {n//1000}k", True,
               f"TTFT {r.ttft:.2f}s => ~{n / r.ttft:,.0f} tok/s prefill")

    # 3. Tool calling through the real API.
    ok_count = 0
    for _ in range(repeats):
        r = chat(base, [{"role": "user", "content": "Turn the kitchen light off."}],
                 tools=TOOL_SCHEMA, max_tokens=300)
        ok, detail = check_tools(r)
        ok_count += ok
    report("tool call (tools API)", ok_count == repeats, f"{ok_count}/{repeats} valid")

    # 4. Tool result round trip -- HA sends the result back and needs a reply.
    first = chat(base, [{"role": "user", "content": "Dim the office light to 30."}],
                 tools=TOOL_SCHEMA, max_tokens=300)
    ok, detail = check_tools(first)
    if ok:
        follow = chat(base, [
            {"role": "user", "content": "Dim the office light to 30."},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": first.tool_calls[0]["name"],
                                          "arguments": first.tool_calls[0]["arguments"]}}]},
            {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'},
        ], tools=TOOL_SCHEMA, max_tokens=120)
        report("tool result round trip", bool(follow.content.strip()) and not follow.error,
               (follow.content.strip()[:56] or follow.error or "empty"))
    else:
        report("tool result round trip", False, f"first call failed: {detail}")

    # 5. Cache reuse. ⛔ The failure this catches is silent: the flag is
    #    accepted, the prompt is reprocessed, and only TTFT reveals it.
    big = filler(6000)
    msgs = [{"role": "user", "content": big + "\nName one function above."}]
    cold = chat(base, msgs, max_tokens=24)
    warm = chat(base, msgs, max_tokens=24)
    if cold.ttft and warm.ttft:
        speedup = cold.ttft / warm.ttft
        report("cache reuse (turn 2)", speedup > 1.5,
               f"cold {cold.ttft:.2f}s -> warm {warm.ttft:.2f}s ({speedup:.1f}x)")
    else:
        report("cache reuse (turn 2)", False, "no TTFT measured")

    # 6. Cache survives an unrelated conversation in between.
    chat(base, [{"role": "user", "content": "What is the capital of France?"}], max_tokens=16)
    again = chat(base, msgs, max_tokens=24)
    if cold.ttft and again.ttft:
        report("cache after interleave", again.ttft < cold.ttft * 0.7,
               f"{again.ttft:.2f}s vs cold {cold.ttft:.2f}s")
    else:
        report("cache after interleave", False, "no TTFT measured")

    # 7. Determinism at temperature 0. exllamav3 #353 reports near-tie flips on
    #    GDN hybrids, which would make a coding assistant irreproducible.
    a = chat(base, [{"role": "user", "content": "Write fizzbuzz in Python. Code only."}],
             max_tokens=200)
    b = chat(base, [{"role": "user", "content": "Write fizzbuzz in Python. Code only."}],
             max_tokens=200)
    report("determinism @ temp 0", a.content == b.content,
           "identical" if a.content == b.content else "DIFFERS between runs")

    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("endpoints", nargs="+",
                    help="Base URLs, e.g. http://10.0.0.118:8000")
    ap.add_argument("--ctx", default="4000,16000",
                    help="Comma-separated prefill prompt sizes in tokens")
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()

    sizes = [int(x) for x in args.ctx.split(",") if x.strip()]
    total = 0
    for base in args.endpoints:
        total += run(base, sizes, args.repeats)
    print(f"\ntotal failures: {total}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
