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


def chat(base: str, messages: list, timeout: int = 2400) -> dict:
    body = {
        "model": "default",
        "messages": messages,
        "temperature": 0,
        "max_tokens": 1200,
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
    answer = None

    for turns in range(1, max_turns + 1):
        try:
            r = chat(base, messages)
        except Exception as e:  # noqa: BLE001
            transcript.append(f"[transport error] {type(e).__name__}: {e}")
            break
        msg = r["choices"][0]["message"]
        messages.append(msg)
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
            out = sandbox_run(cmd)
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

    sandbox_start(a.gguf)
    try:
        res = run(a.base, a.label, a.max_turns)
    finally:
        sandbox_stop()

    print("\n".join(res["transcript"][-14:]))
    print(f"\n=== {a.label}: answer={res['answer']!r} turns={res['turns']} "
          f"tool_calls={res['tool_calls']} malformed={res['malformed_tool_calls']}")
    with open(a.out or f"agent-{a.label}.json", "w") as f:
        json.dump(res, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
