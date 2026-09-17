#!/usr/bin/env python3
"""Compare Home Assistant's live state against hosts/nixos/pelargir/home-assistant-desired.yaml.

⛔ THIS DOES NOT APPLY ANYTHING, AND THAT IS DELIBERATE. Home Assistant's
config entries, pipeline and aliases live in .storage, an application database
HA holds in memory and rewrites on its own schedule. Writing it behind HA's back
loses edits at best and corrupts state at worst. Changes go through Home
Assistant; this tells you whether reality still matches what the fleet expects.

It exists because the rest of this fleet is declarative NixOS with checks that
fail loudly, and the voice assistant's most load-bearing settings are none of
those things. A base_url quietly pointing back at nardol:8000 instead of the
wake gateway works perfectly until the host sleeps, and then every command
fails. That is the class of drift worth catching early.

Runs against the ha-mcp webhook endpoint from inside the Home Assistant pod, so
no credential is ever passed on a command line or printed.

⛔ IT QUERIES LIVE STATE, NEVER .storage ON DISK. Home Assistant batches its
writes, so the files lag whatever is actually in effect — aliases set minutes
earlier still read as `[]` on disk while resolving correctly in conversation.
A drift checker reading those files reports drift that does not exist and,
worse, would miss drift that does.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SPEC = Path(__file__).resolve().parent.parent / "hosts/nixos/pelargir/home-assistant-desired.yaml"
NS = "home"


def sh(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300).stdout


def pod() -> str:
    out = sh(["ssh", "pelargir", f"sudo k3s kubectl -n {NS} get pods -o name"])
    for line in out.splitlines():
        if "home-assistant" in line:
            return line.split("/", 1)[1].strip()
    raise SystemExit("home-assistant pod not found")


def mcp(tool: str, args: dict) -> dict:
    """Call an ha-mcp tool from inside the pod; the credential never leaves it."""
    out = sh(["ssh", "pelargir",
              f"sudo k3s kubectl -n {NS} exec {p_ame} -- "
              f"python /tmp/hamcp.py {tool} '{json.dumps(args)}'"])
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    return {}


def storage(p: str, expr: str) -> object:
    """Read from .storage. ONLY for facts HA does not expose live — config
    entry data. Everything else must come through mcp() above."""
    script = (
        "import json;"
        f"d=json.load(open('/config/.storage/{p}'));"
        f"print(json.dumps({expr}))"
    )
    out = sh(["ssh", "pelargir",
              f"sudo k3s kubectl -n {NS} exec {p_ame} -- python -c \"{script}\""])
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.startswith(("{", "[", '"')):
            return json.loads(line)
    return None


def load_spec() -> dict:
    """Minimal YAML reader: this spec is flat, and a dependency is not worth it."""
    text = SPEC.read_text()
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ModuleNotFoundError:
        pass
    # Fall back to a tiny parser covering the shapes this file actually uses.
    root: dict = {}
    stack = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if line.startswith("- "):
            parent.setdefault("__list__", []).append(line[2:].strip())
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if not val:
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        elif val.startswith("["):
            parent[key] = [v.strip() for v in val.strip("[]").split(",") if v.strip()]
        else:
            parent[key] = val.strip('"')
    return root


def _find_aliases(blob: dict) -> list:
    """ha_get_entity nests its payload differently across versions; find the
    aliases wherever they are rather than pinning one shape."""
    found: list = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "aliases" and isinstance(v, list):
                    found.extend(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(blob)
    return found


FAILURES: list[str] = []
CHECKS = 0


def check(label: str, want, got) -> None:
    global CHECKS
    CHECKS += 1
    ok = str(want) == str(got)
    print(f"  {'ok  ' if ok else 'DRIFT'}  {label:<44} want={want!s:<44} got={got!s}")
    if not ok:
        FAILURES.append(label)


if __name__ == "__main__":
    p_ame = pod()
    spec = load_spec()

    entries = storage("core.config_entries",
                      "[{'domain':e['domain'],'data':e.get('data'),'title':e.get('title')} "
                      "for e in d['data']['entries']]")
    llama = next((e for e in entries if e["domain"] == "llama_cpp"), None)
    conv = spec.get("conversation", {})
    check("llama_cpp base_url", conv.get("base_url"),
          (llama or {}).get("data", {}).get("base_url"))

    pipelines = mcp("ha_manage_pipeline", {"action": "list"}).get("pipelines", [])
    want = spec.get("assist_pipeline", {})
    pipe = next((x for x in pipelines if x.get("name") == want.get("name")), None)
    if pipe is None:
        FAILURES.append("assist pipeline missing")
        print(f"  DRIFT  assist pipeline named {want.get('name')!r} not found")
    else:
        for field in ("conversation_engine", "stt_engine", "tts_engine", "tts_voice"):
            check(f"pipeline.{field}", want.get(field), pipe.get(field))
        check("pipeline.prefer_local_intents", want.get("prefer_local_intents"),
              str(pipe.get("prefer_local_intents")).lower())

    for eid, want_aliases in (spec.get("aliases") or {}).items():
        ent = mcp("ha_get_entity", {"entity_id": eid})
        got = sorted(_find_aliases(ent))
        check(f"aliases {eid}", sorted(want_aliases), got)

    print()
    if FAILURES:
        print(f"  {len(FAILURES)} of {CHECKS} checks drifted: {', '.join(FAILURES)}")
        sys.exit(1)
    print(f"  all {CHECKS} checks match the spec")
