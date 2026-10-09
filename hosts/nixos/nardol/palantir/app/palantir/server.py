"""Palantír's OpenAI-compatible API.

Standard endpoints, so any OpenAI client works unchanged:
  GET  /v1/models                 -> one model, "palantir"
  POST /v1/chat/completions       -> stream or not; videos attach as content
                                     parts {"type":"video_url","video_url":{"url":...}}
                                     (http(s), data:, or file:// under an allowed root)
Palantír-specific, for setup:
  GET/POST /v1/videos             -> list / register (url, path or upload)
  POST /v1/videos/{id}/index      -> precompute every pass in the background
  GET/POST /v1/people             -> list / enroll reference photos
  DELETE   /v1/people/{name}
"""
import asyncio
import json
import os
import re
import threading
import time
import uuid

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import agent, media, models, store, tools

app = FastAPI(title="Palantír")
VID = re.compile(r"\bv_[0-9a-f]{12}\b")


@app.get("/health")
def health():
    return {"ok": True, "models_loaded": models.loaded(), "videos": len(store.videos())}


@app.get("/v1/models")
def list_models():
    return {"object": "list", "data": [{"id": "palantir", "object": "model", "owned_by": "nardol"}]}


def _prepare(messages):
    """Ingest attached videos; replace each with a text reference the agent can use."""
    out, ids = [], []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            parts = []
            for p in content:
                if p.get("type") == "video_url":
                    v = media.ingest(p["video_url"]["url"])
                    ids.append(v["id"])
                    parts.append({"type": "text", "text": f"[attached video {v['id']}: {v['name']}, {v['duration']:.0f} s]"})
                else:
                    parts.append(p)
                    if p.get("type") == "text":
                        ids += VID.findall(p.get("text", ""))
            m = {**m, "content": parts}
        elif isinstance(content, str):
            ids += VID.findall(content)
        out.append(m)
    return out, list(dict.fromkeys(ids))


def _completion(text, trace, model="palantir"):
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:16], "object": "chat.completion", "created": int(time.time()),
        "model": model, "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "palantir_trace": trace,
    }


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    try:
        messages, ids = await run_in_threadpool(_prepare, body.get("messages", []))
    except Exception as e:
        raise HTTPException(400, f"could not load an attached video: {e}")
    if not body.get("stream"):
        text, trace = await run_in_threadpool(agent.run, messages, ids)
        return JSONResponse(_completion(text, trace))

    cid, created = "chatcmpl-" + uuid.uuid4().hex[:16], int(time.time())

    def chunk(delta, finish=None):
        return "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created,
                                      "model": "palantir", "choices": [{"index": 0, "delta": delta,
                                                                        "finish_reason": finish}]}) + "\n\n"

    async def gen():
        yield chunk({"role": "assistant"})
        task = asyncio.create_task(run_in_threadpool(agent.run, messages, ids))
        # First-time indexing of a long video can take minutes; keep the
        # connection alive with SSE comments, which OpenAI clients ignore.
        while not task.done():
            await asyncio.sleep(10)
            if not task.done():
                yield ": working\n\n"
        try:
            text, _ = task.result()
        except Exception as e:
            text = f"Palantír failed: {e}"
        yield chunk({"content": text})
        yield chunk({}, "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/v1/videos")
def list_videos():
    return {"data": store.videos()}


def _background_index(vid):
    threading.Thread(target=tools.index_all, args=(vid,), daemon=True).start()


@app.post("/v1/videos")
async def add_video(request: Request):
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/"):
        form = await request.form()
        up = form["file"]
        inbox = os.path.join(store.DATA, "inbox")
        os.makedirs(inbox, exist_ok=True)
        path = os.path.join(inbox, os.path.basename(up.filename or "upload.mp4"))
        with open(path, "wb") as f:
            f.write(await up.read())
        source, index = path, form.get("index") in ("1", "true")
    else:
        body = await request.json()
        source, index = body.get("url") or body.get("path"), bool(body.get("index"))
    try:
        v = await run_in_threadpool(media.ingest, source)
    except Exception as e:
        raise HTTPException(400, str(e))
    if index:
        _background_index(v["id"])
    return v


@app.post("/v1/videos/{vid}/index")
def index_video(vid: str):
    if store.video(vid) is None:
        raise HTTPException(404, "unknown video")
    _background_index(vid)
    return {"id": vid, "indexing": True}


@app.get("/v1/people")
def people():
    return {"data": tools.people_list()}


@app.post("/v1/people")
async def enroll(name: str = Form(...), files: list[UploadFile] = File(...)):
    data = [await f.read() for f in files]
    added = await run_in_threadpool(tools.enroll, name, data)
    if not added:
        raise HTTPException(400, "no face found in any of the images")
    return {"name": name, "added": added, "photos": len(data)}


@app.delete("/v1/people/{name}")
def forget(name: str):
    return {"name": name, "removed": tools.forget_person(name)}
