#!/usr/bin/env python3
"""A minimal agentic loop against nardol's endpoint, for ONE hard task.

Not the harness. The harness is the repeatable, many-task, scored version; this
is the single-task probe that tells you whether building it is worth it.

The agent gets one tool -- run a shell command -- and that command executes
inside a disposable container on nardol with NO NETWORK and the target file
mounted read-only. So the model must actually work the problem: inspect bytes,
form a hypothesis about the format, write code, run it, read the error, and
iterate. None of which a single-prompt benchmark can see.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request

SANDBOX = "agentbox"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run a shell command in the sandbox and return its stdout and stderr. "
                "Python 3 is available. The working directory is /work (writable). "
                "There is no network access."
            ),
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
]


def sandbox_start(gguf: str) -> None:
    subprocess.run(
        ["ssh", "nardol", f"sudo -n docker rm -f {SANDBOX} >/dev/null 2>&1; true"],
        capture_output=True, text=True, timeout=120,
    )
    subprocess.run(
        [
            "ssh", "nardol",
            f"sudo -n docker run -d --name {SANDBOX} --network none "
            f"-v {gguf}:/data/model.gguf:ro "
            f"-w /work python:3.12-slim sleep 7200 >/dev/null 2>&1; "
            f"sudo -n docker exec {SANDBOX} mkdir -p /work",
        ],
        capture_output=True, text=True, timeout=300,
    )


def sandbox_run(command: str) -> str:
    """Execute in the sandbox. Output is truncated: a hexdump can be enormous.

    ⛔ THE COMMAND GOES OVER STDIN, NOT AS AN ARGUMENT. Passing it as a quoted
    argument sends it through ssh's shell and then bash -lc, and a multi-line
    script arrives with its newlines as literal backslash-n -- which is a syntax
    error the moment the model writes a real script rather than a one-liner.
    The first run of this harness scored that as the model failing; it was the
    transport. Anything that mangles a tool's input measures the harness.
    """
    r = subprocess.run(
        ["ssh", "nardol", f"sudo -n timeout 60 docker exec -i {SANDBOX} bash -s"],
        input=command, capture_output=True, text=True, timeout=180,
    )
    out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr.strip() else "")
    return out[:4000] if out.strip() else "(no output)"


def sandbox_stop() -> None:
    subprocess.run(["ssh", "nardol", f"sudo -n docker rm -f {SANDBOX} >/dev/null 2>&1; true"],
                   capture_output=True, text=True, timeout=120)


def chat(base: str, messages: list, timeout: int = 2400, max_tokens: int = 3000) -> dict:
    body = {
        "model": "default",
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
        "tools": TOOLS,
        "tool_choice": "auto",
    }
    req = urllib.request.Request(
        f"{base}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


TASK = """You have a file at /data/model.gguf. It is a GGUF file -- the binary format
used by llama.cpp -- but it is ONLY the first shard of a split model, so it contains the
header and metadata and no tensor data.

Determine these four facts about the model, by parsing the binary yourself:
  1. the architecture string
  2. the number of transformer blocks
  3. the total number of experts
  4. the number of experts used per token

⛔ Do NOT install or import any GGUF/llama library -- there is no network and none is
present. Parse the bytes with the standard library. Use the bash tool to explore the file
and to run code; iterate until you are confident.

When you are done, reply with a final message containing exactly one line:
ANSWER: <architecture> <blocks> <total_experts> <experts_used>
"""


def run(base: str, label: str, max_turns: int) -> dict:
    messages = [{"role": "user", "content": TASK}]
    transcript, turns, tool_calls, bad_calls = [], 0, 0, 0
    last_cmd, repeats = [None], [0]
    # ⛔ TURNS ARE NOT TIME. A model at 4x the decode rate can still lose on
    # wall-clock by taking 3x the turns, and turns alone hide that completely.
    # model_s is time spent waiting on the model; tool_s is sandbox execution,
    # which is the same for both and must not be charged to either.
    t0 = time.time()
    model_s, tool_s = 0.0, 0.0
    answer = None

    for turns in range(1, max_turns + 1):
        try:
            _m0 = time.time()
            r = chat(base, messages)
            model_s += time.time() - _m0
        except Exception as e:  # noqa: BLE001
            # Dump the exact request that failed. A 500 here is a server-side
            # template error and the response body says nothing useful, so the
            # only way to find the trigger is to keep the payload that caused it.
            with open(f"failed-request-{label}.json", "w") as fh:
                json.dump({"error": f"{type(e).__name__}: {e}", "messages": messages},
                          fh, indent=2)
            transcript.append(f"[transport error] {type(e).__name__}: {e} "
                              f"(request saved to failed-request-{label}.json)")
            break
        msg = r["choices"][0]["message"]
        # ⛔ AND NEVER PUT AN UNPARSEABLE TOOL CALL INTO HISTORY. A model writing
        # a whole program inside a JSON string argument can be truncated by
        # max_tokens mid-string; echoing that back makes the NEXT request fail
        # with HTTP 500 from the server's template, which looks like an engine
        # fault and is actually a poisoned transcript. Keep the text, drop the
        # broken call, and tell the model what happened.
        _raw = msg.get("tool_calls") or []
        _broken = []
        for _c in _raw:
            try:
                json.loads(_c["function"]["arguments"])
            except Exception:  # noqa: BLE001
                _broken.append(_c)
        if _broken:
            bad_calls += len(_broken)
            messages.append({"role": "assistant",
                             "content": (msg.get("content") or "").strip()
                             or "(tool call was truncated)"})
            messages.append({"role": "user", "content":
                             "Your last tool call was truncated and could not be parsed. "
                             "Write long files in several smaller appends rather than one "
                             "large heredoc, then continue."})
            transcript.append(f"--- turn {turns} truncated tool call ({len(_broken)}) ---")
            continue
        # ⛔ NEVER ECHO THE SERVER'S MESSAGE OBJECT VERBATIM. ik's server returns
        # assistant messages carrying `reasoning_content`, and sending one back
        # with that key set to null makes the NEXT request fail with HTTP 500 --
        # a server-side template error, not a client one. It only fires when the
        # model emits a text-only turn, so an agent loop can run for hours and
        # then die the first time the model thinks out loud without calling a
        # tool. Forward only what the protocol needs.
        messages.append({
            k: v for k, v in msg.items()
            if k in ("role", "content", "tool_calls") and v is not None
        } or {"role": "assistant", "content": ""})
        content = (msg.get("content") or "").strip()
        calls = msg.get("tool_calls") or []

        if content:
            transcript.append(f"--- turn {turns} says ---\n{content[:600]}")
        if "ANSWER:" in content:
            answer = content.split("ANSWER:", 1)[1].strip().splitlines()[0].strip()
            break
        if not calls:
            # No tool call and no answer: nudge once, then give up on this turn.
            messages.append({"role": "user", "content":
                             "Continue. Use the bash tool, or give the final ANSWER: line."})
            continue

        for c in calls:
            tool_calls += 1
            try:
                args = json.loads(c["function"]["arguments"])
                cmd = args["command"]
            except Exception:  # noqa: BLE001
                bad_calls += 1
                messages.append({"role": "tool", "tool_call_id": c.get("id", ""),
                                 "content": "malformed arguments; send {\"command\": \"...\"}"})
                continue
            _t0 = time.time()
            out = sandbox_run(cmd)
            tool_s += time.time() - _t0
            if cmd == last_cmd[0]:
                repeats[0] += 1
                out += (
                    f"\n[harness] identical command repeated {repeats[0]}x with the same "
                    "result. Change the approach: write the script to a file first, or "
                    "simplify to a one-liner."
                )
            else:
                repeats[0] = 0
            last_cmd[0] = cmd
            transcript.append(f"--- turn {turns} ran ---\n$ {cmd[:300]}\n{out[:600]}")
            messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": out})

    return {
        "label": label,
        "answer": answer,
        "turns": turns,
        "tool_calls": tool_calls,
        "malformed_tool_calls": bad_calls,
        "max_identical_repeats": repeats[0],
        "wall_s": round(time.time() - t0, 1),
        "model_s": round(model_s, 1),
        "tool_s": round(tool_s, 1),
        "transcript": transcript,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("label")
    ap.add_argument("--gguf",
                    default="/srv/inference/gguf/flash-next/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf")
    ap.add_argument("--max-turns", type=int, default=14)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    # ⛔ ONE SANDBOX PER RUN. A fixed name meant a second run's cleanup
    # destroyed the first run's container mid-flight, and the victim burned
    # every remaining turn on tool errors that looked like model failures.
    globals()['SANDBOX'] = f"agentbox-{a.label}"

    sandbox_start(a.gguf)
    try:
        res = run(a.base, a.label, a.max_turns)
    finally:
        sandbox_stop()

    print("\n".join(res["transcript"][-14:]))
    print(f"\n=== {a.label}: answer={res['answer']!r} turns={res['turns']} "
          f"wall={res['wall_s']}s model={res['model_s']}s tool={res['tool_s']}s "
          f"malformed={res['malformed_tool_calls']}")
    with open(a.out or f"agent-{a.label}.json", "w") as f:
        json.dump(res, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
