#!/usr/bin/env python3
"""Quality battery for nardol's inference endpoint.

Answers two questions the throughput work could not:
  1. does KV precision (q4_0 vs iq4_nl vs q8_0) change what comes out?
  2. is the 125B offload model actually better than the 27B at coding?

Design notes, each learned the hard way earlier in this session:

  - SCORING IS AUTOMATED OR IT IS NOTHING. Generated code runs against hidden
    tests; needles are exact-match; tool calls are validated against the schema.
    The one subjective task records its output for a human to grade and is
    excluded from the headline score.
  - IDENTICAL PROMPTS, temperature 0, per-task seeds derived from the task name
    only -- never from the config -- so two configs see byte-identical inputs.
  - REPEATS ON ANYTHING THAT VARIES. Throughput on this box has a +/-6% noise
    floor; pass rates on 10 tasks have a far worse one. n>=3 for codegen.
  - DEPTH IS A FIRST-CLASS VARIABLE. The KLD literature says quantized KV does
    its damage on long documents and tool calls, not on short prompts, so the
    needle and long-document tasks run at depth rather than at 512 tokens.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.request

# --- code generation: hidden tests, objective pass/fail ---------------------
CODEGEN = [
    (
        "rle",
        "Write a Python function `encode(s: str) -> str` that run-length encodes a "
        "string: 'aaab' -> 'a3b1'. Single characters still get a count. "
        "Respond with ONLY the function in a ```python code block.",
        "assert encode('aaab')=='a3b1'\nassert encode('')==''\nassert encode('x')=='x1'",
    ),
    (
        "balance",
        "Write a Python function `balanced(s: str) -> bool` returning True if the "
        "brackets ()[]{} in s are balanced and correctly nested. Ignore other "
        "characters. Respond with ONLY the function in a ```python code block.",
        "assert balanced('a(b[c]{d})')\nassert not balanced('(]')\nassert balanced('')\n"
        "assert not balanced('(')",
    ),
    (
        "merge_intervals",
        "Write a Python function `merge(iv: list[tuple[int,int]]) -> list[tuple[int,int]]` "
        "that merges overlapping intervals and returns them sorted by start. "
        "Touching intervals like (1,2) and (2,3) merge. "
        "Respond with ONLY the function in a ```python code block.",
        "assert merge([(1,3),(2,6),(8,10)])==[(1,6),(8,10)]\n"
        "assert merge([])==[]\nassert merge([(2,3),(1,2)])==[(1,3)]",
    ),
    (
        "version_cmp",
        "Write a Python function `newer(a: str, b: str) -> bool` that returns True if "
        "dotted version string a is strictly newer than b. Segments are integers and "
        "the strings may have different lengths ('1.2' vs '1.2.0' are equal). "
        "Respond with ONLY the function in a ```python code block.",
        "assert newer('1.10','1.9')\nassert not newer('1.2','1.2.0')\n"
        "assert newer('2.0.1','2.0')\nassert not newer('1.0','1.0.1')",
    ),
    (
        "retry_backoff",
        "Write a Python function `delays(n: int, base: float, cap: float) -> list[float]` "
        "returning n exponential backoff delays starting at base, doubling each time, "
        "each clamped to cap. Respond with ONLY the function in a ```python code block.",
        "assert delays(4,1.0,8.0)==[1.0,2.0,4.0,8.0]\n"
        "assert delays(4,1.0,3.0)==[1.0,2.0,3.0,3.0]\nassert delays(0,1.0,2.0)==[]",
    ),
    (
        "parse_kv",
        "Write a Python function `parse(text: str) -> dict[str,str]` that parses lines of "
        "the form key=value into a dict. Lines that are blank or start with # are skipped. "
        "Only the FIRST '=' splits. Later duplicate keys win. "
        "Respond with ONLY the function in a ```python code block.",
        "assert parse('a=1\\n#c=2\\n\\nb=x=y')=={'a':'1','b':'x=y'}\n"
        "assert parse('')=={}\nassert parse('k=1\\nk=2')=={'k':'2'}",
    ),
    (
        "topo",
        "Write a Python function `order(deps: dict[str,list[str]]) -> list[str]` that "
        "returns a topological order where each key comes after all of its dependencies. "
        "Return [] if there is a cycle. Respond with ONLY the function in a ```python code block.",
        "r=order({'a':[],'b':['a'],'c':['b']})\nassert r.index('a')<r.index('b')<r.index('c')\n"
        "assert order({'a':['b'],'b':['a']})==[]",
    ),
    (
        "chunk_tokens",
        "Write a Python function `chunks(xs: list[int], n: int) -> list[list[int]]` "
        "splitting xs into consecutive chunks of at most n items. n<=0 raises ValueError. "
        "Respond with ONLY the function in a ```python code block.",
        "assert chunks([1,2,3,4,5],2)==[[1,2],[3,4],[5]]\nassert chunks([],3)==[]\n"
        "try:\n    chunks([1],0)\n    raise AssertionError('expected ValueError')\n"
        "except ValueError:\n    pass",
    ),
]

HARD_CODEGEN = [
    (
        "interval_subtract",
        "Write `subtract(a: list[tuple[int,int]], b: list[tuple[int,int]]) -> list[tuple[int,int]]` "
        "returning the half-open intervals in a not covered by any interval in b, sorted. "
        "Inputs may be unsorted and overlapping. Respond with ONLY the function in a ```python block.",
        "assert subtract([(1,10)],[(3,5)])==[(1,3),(5,10)]\n"
        "assert subtract([(1,5),(4,8)],[(2,3)])==[(1,2),(3,8)]\n"
        "assert subtract([(1,4)],[(0,9)])==[]\n"
        "assert subtract([(1,4)],[])==[(1,4)]",
    ),
    (
        "semver_range",
        "Write `satisfies(v: str, spec: str) -> bool` for semver where spec is one of "
        "'>=X.Y.Z', '^X.Y.Z' (compatible: same major, >= given; if major is 0 then same minor), "
        "or an exact 'X.Y.Z'. Respond with ONLY the function in a ```python block.",
        "assert satisfies('1.4.0','^1.2.3')\nassert not satisfies('2.0.0','^1.2.3')\n"
        "assert not satisfies('0.3.0','^0.2.1')\nassert satisfies('0.2.9','^0.2.1')\n"
        "assert satisfies('1.2.3','1.2.3')\nassert not satisfies('1.2.2','>=1.2.3')",
    ),
    (
        "ledger_reconcile",
        "Write `reconcile(entries: list[dict]) -> dict[str,int]` where each entry has keys "
        "'account' (str), 'delta' (int) and optional 'reverses' (an index into entries). "
        "An entry with 'reverses' cancels that earlier entry entirely AND is itself not applied. "
        "Return final balances, omitting accounts whose balance is 0. "
        "Respond with ONLY the function in a ```python block.",
        "e=[{'account':'a','delta':5},{'account':'b','delta':3},{'account':'x','delta':0,'reverses':0}]\n"
        "assert reconcile(e)=={'b':3}\n"
        "assert reconcile([{'account':'a','delta':2},{'account':'a','delta':-2}])=={}",
    ),
    (
        "window_dedup",
        "Write `dedup(xs: list[int], w: int) -> list[int]` keeping an item only if it did not "
        "appear within the previous w KEPT items (not raw positions). w=0 keeps everything. "
        "Respond with ONLY the function in a ```python block.",
        # ⛔ [5] NOT [5,5]. The original expectation here was wrong and every
        # model "failed" it, which is the signature of a broken assertion
        # rather than a model limitation -- check the reference by hand before
        # believing a task that everything fails.
        "assert dedup([1,2,1,3,1],2)==[1,2,3,1]\n"
        "assert dedup([1,1,1],0)==[1,1,1]\nassert dedup([],3)==[]\n"
        "assert dedup([5,5,5,5],1)==[5]",
    ),
]

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_query",
            "description": "Run a read-only SQL query against a named database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "database": {"type": "string"},
                    "sql": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                "required": ["database", "sql"],
            },
        },
    }
]


def post(base: str, path: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def chat(base: str, messages: list, timeout: int = 1800, tools=None, max_tokens=900) -> dict:
    body = {
        "model": "default",
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    return post(base, "/v1/chat/completions", body, timeout)


def extract_code(text: str) -> str:
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    return m.group(1) if m else text


def run_tests(code: str, tests: str) -> bool:
    """Execute generated code against hidden asserts, isolated and time-boxed."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code + "\n\n" + tests + "\nprint('OK')\n")
        path = f.name
    try:
        r = subprocess.run(
            [sys.executable, path], capture_output=True, text=True, timeout=20
        )
        return r.returncode == 0 and "OK" in r.stdout
    except Exception:
        return False


def filler(tokens: int, seed: int) -> str:
    """Deterministic haystack. Same bytes for every config."""
    # Calibrated against /tokenize on 2026-09-17: this generator emits 1.21
    # real tokens per unit of `tokens`, so an uncorrected filler(170000) is
    # ~205k tokens and the server rejects it with HTTP 400 -- which scores as a
    # retrieval MISS unless you look at the error. Scale here, once.
    tokens = int(tokens / 1.21)
    state = seed * 6364136223846793005 + 1442695040888963407
    words = (
        "deploy manifest rollback quorum replica shard ledger invoice cursor "
        "backoff jitter snapshot artifact tenant region schema migration"
    ).split()
    out, n = [], 0
    while n < tokens:
        state = (state * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
        w = words[(state >> 33) % len(words)]
        k = (state >> 17) % 997
        out.append(f"- record {k}: the {w} for shard {k} completed in {k % 97} ms\n")
        n += 18
    return "".join(out)


def task_codegen(base: str, reps: int, suite=None) -> dict:
    passed = total = 0
    detail = {}
    for name, prompt, tests in (suite or CODEGEN):
        oks = 0
        for _ in range(reps):
            try:
                r = chat(base, [{"role": "user", "content": prompt}])
                code = extract_code(r["choices"][0]["message"]["content"] or "")
                oks += 1 if run_tests(code, tests) else 0
            except Exception:
                pass
        detail[name] = f"{oks}/{reps}"
        passed += oks
        total += reps
    return {"score": f"{passed}/{total}", "pct": round(100 * passed / total, 1), "detail": detail}


def task_needle(base: str, depths: list[int]) -> dict:
    """Retrieval at depth, WITH DISTRACTORS.

    A lone needle is trivially found and saturates at 1/1, which cannot separate
    two KV precisions. Three similar-looking keys are planted at different
    depths and only one matches the question, so a model whose attention has
    degraded returns a plausible WRONG key rather than failing loudly.
    """
    hits, out = 0, {}
    for d in depths:
        want = f"RAVEN-{7 * d % 9973}"
        decoys = [f"RAVEN-{(7 * d % 9973) + k}" for k in (1, 2)]
        hay = filler(d, seed=d)
        q = len(hay) // 4
        doc = (
            hay[:q]
            + f"\n- NOTE: the STAGING passphrase is {decoys[0]}\n"
            + hay[q : 2 * q]
            + f"\n- NOTE: the ARCHIVE passphrase is {want}\n"
            + hay[2 * q : 3 * q]
            + f"\n- NOTE: the BACKUP passphrase is {decoys[1]}\n"
            + hay[3 * q :]
        )
        msg = [
            {
                "role": "user",
                "content": doc
                + "\n\nWhat is the ARCHIVE passphrase (not staging, not backup)? "
                "Reply with only the passphrase.",
            }
        ]
        try:
            r = chat(base, msg, max_tokens=40)
            got = (r["choices"][0]["message"]["content"] or "").strip()
            ok = want in got and not any(x in got for x in decoys)
        except Exception as e:  # noqa: BLE001
            got, ok = f"ERROR {type(e).__name__}", False
        hits += ok
        out[f"{d // 1000}k"] = "hit" if ok else f"MISS({got[:30]})"
    return {"score": f"{hits}/{len(depths)}", "detail": out}


def task_tools(base: str, depth: int) -> dict:
    """Tool calling, at depth -- the other place quantized KV is reported to hurt."""
    pre = filler(depth, seed=99) if depth else ""
    msg = [
        {
            "role": "user",
            "content": pre
            + "\n\nUsing the tool, fetch at most 5 rows of the id and email columns "
            "from the customers table in the 'billing' database.",
        }
    ]
    try:
        r = chat(base, msg, tools=TOOLS, max_tokens=300)
        m = r["choices"][0]["message"]
        calls = m.get("tool_calls") or []
        if not calls:
            return {"ok": False, "why": "no tool_calls", "content": (m.get("content") or "")[:120]}
        fn = calls[0]["function"]
        args = json.loads(fn["arguments"])
        ok = (
            fn["name"] == "run_query"
            and args.get("database") == "billing"
            and isinstance(args.get("sql"), str)
            and "customers" in args["sql"].lower()
            and args.get("limit") == 5
        )
        return {"ok": ok, "args": args}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}


def task_json(base: str) -> dict:
    msg = [
        {
            "role": "user",
            "content": "Return ONLY a JSON object, no prose and no code fence, with keys "
            '"service" (string), "port" (integer), "tls" (boolean) describing an HTTPS '
            "service named gateway on the standard port.",
        }
    ]
    try:
        r = chat(base, msg, max_tokens=200)
        txt = (r["choices"][0]["message"]["content"] or "").strip()
        txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt).strip()
        o = json.loads(txt)
        ok = (
            isinstance(o.get("service"), str)
            and o.get("port") == 443
            and o.get("tls") is True
        )
        return {"ok": ok, "got": o}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}


def task_review(base: str) -> dict:
    """Subjective: recorded for human grading, excluded from the headline score.

    The defect is real and load-bearing: StartLimit* are [Unit] directives and
    systemd ignores them under [Service], so this rate limit never applies.
    """
    snippet = """systemd.services."docker-ikllama" = {
  serviceConfig = {
    startLimitBurst = 5;
    startLimitIntervalSec = 600;
    RestartSec = "30s";
  };
};"""
    msg = [
        {
            "role": "user",
            "content": "This NixOS module intends to rate-limit restarts of a systemd "
            "service to 5 failures per 600 seconds. It does not work. Explain the defect "
            "in two sentences.\n\n```nix\n" + snippet + "\n```",
        }
    ]
    try:
        r = chat(base, msg, max_tokens=300)
        txt = (r["choices"][0]["message"]["content"] or "").strip()
        # Keyword signal only; the text is what a human grades.
        hit = bool(re.search(r"\[?unit\]?", txt, re.I)) and bool(
            re.search(r"serviceConfig|\[Service\]", txt, re.I)
        )
        return {"keyword_signal": hit, "text": txt[:400]}
    except Exception as e:  # noqa: BLE001
        return {"keyword_signal": False, "text": f"ERROR {type(e).__name__}: {e}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base", help="e.g. http://10.0.0.118:8001")
    ap.add_argument("--label", default="run")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--depths", default="8000,32000")
    ap.add_argument("--tool-depth", type=int, default=32000)
    ap.add_argument("--only", default="", help="comma list: codegen,needle,tools,json,review")
    ap.add_argument("--out", default="quality.json")
    a = ap.parse_args()

    depths = [int(x) for x in a.depths.split(",") if x.strip()]
    res = {"label": a.label}
    print(f"=== {a.label} ===", flush=True)
    only = [x for x in a.only.split(",") if x]
    want = lambda k: (not only) or k in only
    if want("codegen"):
        res["codegen"] = task_codegen(a.base, a.reps)
        print("  codegen ", res["codegen"]["score"], res["codegen"]["detail"], flush=True)
        res["codegen_hard"] = task_codegen(a.base, a.reps, HARD_CODEGEN)
        print("  hard    ", res["codegen_hard"]["score"], res["codegen_hard"]["detail"], flush=True)
    if want("needle"):
        res["needle"] = task_needle(a.base, depths)
        print("  needle  ", res["needle"]["score"], res["needle"]["detail"], flush=True)
    if want("tools"):
        res["tools_deep"] = task_tools(a.base, a.tool_depth)
        print("  tools   ", res["tools_deep"], flush=True)
    if want("json"):
        res["json"] = task_json(a.base)
        print("  json    ", res["json"], flush=True)
    if want("review"):
        res["review"] = task_review(a.base)
        print("  review  keyword_signal=", res["review"]["keyword_signal"], flush=True)

    with open(a.out, "w") as f:
        json.dump(res, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
