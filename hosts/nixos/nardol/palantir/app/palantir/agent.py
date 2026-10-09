"""The loop that lets GLM answer by calling tools."""
import json
import os
import re

import httpx

from . import store, tools

GLM = os.environ.get("PALANTIR_GLM", "http://127.0.0.1:8000/v1")
MAX_STEPS = int(os.environ.get("PALANTIR_MAX_STEPS", "8"))

SYSTEM = """You are Palantír. You answer questions about a family's home videos by calling tools; you cannot see a video unless a tool shows it to you.

How to work:
- "What is this about?": call overview first, then look or watch where it matters.
- Details (faces, text, objects): look. Motion, actions, order of events: watch. Speech: listen. Finding a moment: search, then look to confirm.
- Whether a person appears is decided ONLY by find_person. If it returns NOT FOUND or says the person is not enrolled, say exactly that. Never identify a person by appearance, and never guess a name.
- When find_person returns several segments, look at (or watch) each of them before describing what the person does; the interesting moment is often not the first.
- find_person frames box the matched face in red: that boxed face is the person; other faces in the frame are other people.
- Answer in a few specific sentences: what happens, who (only as find_person established), and when, with times as m:ss. Do not mention tools, functions or your process in the answer. When you have enough, answer without calling more tools.

Videos in this conversation: {videos}"""

# ⛔ THINKING ON FOR THE AGENT, OFF FOR EVERYONE ELSE. The GLM profile's
# server default is non-thinking because Home Assistant cannot switch it per
# request; Palantír can, and multi-step tool planning is where a 9B model needs
# it (without it, it returned its own plan as the answer and misread times).
THINK = {"chat_template_kwargs": {"enable_thinking": True}}

# Tools whose `video` argument is required; find_person and search treat a
# missing one as "every video" and are deliberately not listed.
NEEDS_VIDEO = {"overview", "look", "watch", "listen"}

BOX = re.compile(r"<\|/?(begin|end)_of_box\|>")


def _system(video_ids):
    vs = [store.video(v) for v in video_ids]
    listed = "; ".join(f"{v['id']} ({v['name']}, {v['duration']:.0f} s)" for v in vs if v) or "none attached (use list_videos)"
    return SYSTEM.format(videos=listed)


def _post(payload):
    r = httpx.post(f"{GLM}/chat/completions", json=payload, timeout=900)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


def _prune(convo, keep):
    """Keep the newest attachment turn whole; older frames cost context.

    ⛔ EXCEPT find_person's boxed frames, which are identity evidence. Pruning
    them let a later unboxed `look` frame stand alone, and GLM attributed
    another person's action to the one it was asked about (2026-10-08).
    """
    out = []
    for i, m in enumerate(convo):
        if m.get("_attachments") and i != keep and not m.get("_identity"):
            out.append({"role": "user", "content": "[frames or clips from an earlier step omitted to save context]"})
        else:
            out.append({k: v for k, v in m.items() if not k.startswith("_")})
    return out


def _clean(text):
    return BOX.sub("", text or "").strip()


def run(messages, video_ids):
    convo = [{"role": "system", "content": _system(video_ids)}] + messages
    trace, last_att, failures = [], -1, {}
    for _ in range(MAX_STEPS):
        msg = _post({"model": "default", "messages": _prune(convo, last_att), "tools": tools.SPEC,
                     "tool_choice": "auto", "max_tokens": 4000, **THINK})
        calls = msg.get("tool_calls") or []
        if not calls:
            return _clean(msg.get("content")), trace
        convo.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        attachments = []
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            # A small model often drops the video id. With exactly one video in
            # the conversation there is no ambiguity, so fill it in; tools that
            # search everything when it is omitted keep that meaning.
            if name in NEEDS_VIDEO and not args.get("video") and len(video_ids) == 1:
                args["video"] = video_ids[0]
            key = (name, json.dumps(args, sort_keys=True))
            try:
                text, att = tools.TOOLS[name](**args)
            except Exception as e:  # report the failure to the model, let it adapt
                text, att = f"error: {e}", []
                failures[key] = failures.get(key, 0) + 1
                if failures[key] > 1:
                    text += " This exact call has already failed; do not repeat it. Change the arguments or answer."
            trace.append({"tool": name, "args": args, "result": text[:500]})
            convo.append({"role": "tool", "tool_call_id": c["id"], "content": text})
            if att:
                attachments += [{"type": "text", "text": f"[{name} output]"}] + att
        if attachments:
            convo.append({"role": "user", "content": attachments, "_attachments": True,
                          "_identity": any(c["function"]["name"] == "find_person" for c in calls)})
            last_att = len(convo) - 1
    # Out of steps: ask for the best answer from what was gathered.
    msg = _post({"model": "default", "messages": _prune(convo, last_att) + [
        {"role": "user", "content": "Answer now from what you have gathered, without calling tools."}],
        "max_tokens": 4000, **THINK})
    return _clean(msg.get("content")), trace
