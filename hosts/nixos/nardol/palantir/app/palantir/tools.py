"""The tools GLM can call, and the index passes behind them.

Each tool returns (text, attachments). Text goes back as the tool result;
attachments (frames, clips) are shown to the model in a follow-up user turn,
because tool results are text-only in the OpenAI protocol.

⛔ IDENTITY COMES ONLY FROM find_person. Every vision model tested on this host
(Qwen3.8, GLM-4.6V at 9B and 106B, Gemma 4) named the closest lookalike when
the right person was not among its references. Face embeddings with a
threshold can say "not found"; the language model cannot.
"""
import base64
import os
import threading

import numpy as np

from . import media, models, store

FACE_THRESHOLD = float(os.environ.get("PALANTIR_FACE_THRESHOLD", "0.45"))
FACE_STEP = float(os.environ.get("PALANTIR_FACE_STEP", "1.0"))
FRAME_STEP = float(os.environ.get("PALANTIR_FRAME_STEP", "2.0"))
MAX_WATCH = 90.0
_index_lock = threading.Lock()


def _img(jpeg):
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}}


def _fmt(t):
    # Whole seconds only: GLM read "0:03.5" as 3:50 (2026-10-08).
    t = int(round(max(0.0, float(t))))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def _every(video):
    """Models say "all" or "*" when they mean "search every video"."""
    return video in (None, "", "all", "*", "any", "every", "ALL")


def _need(vid):
    v = store.video(vid)
    if v is None:
        raise ValueError(f"unknown video {vid}; call list_videos")
    return v


# ── index passes ──────────────────────────────────────────────────────────

def ensure_scenes(vid):
    with _index_lock:
        if store.stage_done(vid, "scenes"):
            return
        v = _need(vid)
        rows = [(vid, i, s, e) for i, (s, e) in enumerate(media.scenes(v["path"]))]
        with store.db() as c:
            c.execute("delete from scenes where video=?", (vid,))
            c.executemany("insert into scenes values(?,?,?,?)", rows)
        store.mark_done(vid, "scenes")


def ensure_faces(vid):
    with _index_lock:
        if store.stage_done(vid, "faces"):
            return
        v = _need(vid)
        app = models.faces()
        rows = []
        for t, rgb in media.frames_rgb(v["path"], step=FACE_STEP):
            for f in app.get(rgb[:, :, ::-1].copy()):
                x1, y1, x2, y2 = (float(x) for x in f.bbox)
                if f.det_score < 0.5 or min(x2 - x1, y2 - y1) < 20:
                    continue
                rows.append((vid, t, x1, y1, x2, y2, float(f.det_score), store.blob(f.normed_embedding)))
        with store.db() as c:
            c.execute("delete from faces where video=?", (vid,))
            c.executemany("insert into faces values(?,?,?,?,?,?,?,?)", rows)
        store.mark_done(vid, "faces")


def ensure_transcript(vid):
    with _index_lock:
        if store.stage_done(vid, "transcript"):
            return
        v = _need(vid)
        audio = media.audio_wav(v["path"])
        rows, lang = [], ("none", 0.0)
        if audio is not None and len(audio):
            # Whisper invents text over music, singing and noise. Keep only
            # segments it is confident are speech, and never condition on its
            # own previous output (that is how one hallucination becomes ten).
            segs, info = models.whisper().transcribe(
                audio, vad_filter=True, beam_size=5, condition_on_previous_text=False)
            lang = (info.language, info.language_probability)
            rows = [(vid, s.start, s.end, s.text.strip()) for s in segs
                    if s.text.strip() and s.avg_logprob > -1.0 and s.no_speech_prob < 0.6]
        rows.append((vid, -1.0, -1.0, f"__lang__ {lang[0]} {lang[1]:.2f}"))
        with store.db() as c:
            c.execute("delete from transcript where video=?", (vid,))
            c.executemany("insert into transcript values(?,?,?,?)", rows)
        store.mark_done(vid, "transcript")


def _siglip_images(images):
    import torch
    model, proc = models.siglip()
    with torch.no_grad():
        inp = proc(images=images, return_tensors="pt").to("cuda", torch.float16)
        e = model.get_image_features(**inp).float()
    return torch.nn.functional.normalize(e, dim=-1).cpu().numpy()


def ensure_frames(vid):
    with _index_lock:
        if store.stage_done(vid, "frames"):
            return
        from PIL import Image
        v = _need(vid)
        rows, batch, times = [], [], []
        for t, rgb in media.frames_rgb(v["path"], step=FRAME_STEP, max_side=512):
            batch.append(Image.fromarray(rgb)); times.append(t)
            if len(batch) == 32:
                rows += [(vid, tt, store.blob(e)) for tt, e in zip(times, _siglip_images(batch))]
                batch, times = [], []
        if batch:
            rows += [(vid, tt, store.blob(e)) for tt, e in zip(times, _siglip_images(batch))]
        with store.db() as c:
            c.execute("delete from frames where video=?", (vid,))
            c.executemany("insert into frames values(?,?,?)", rows)
        store.mark_done(vid, "frames")


def index_all(vid):
    for f in (ensure_scenes, ensure_transcript, ensure_faces, ensure_frames):
        f(vid)


# ── people ────────────────────────────────────────────────────────────────

def enroll(name, images):
    """Add reference photos (JPEG/PNG bytes) for a person; largest face each."""
    import cv2
    app = models.faces()
    added = 0
    for data in images:
        bgr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        found = app.get(bgr)
        if not found:
            continue
        f = max(found, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        with store.db() as c:
            c.execute("insert into people values(?,?,?,strftime('%s','now'))",
                      (name, store.blob(f.normed_embedding), "upload"))
        added += 1
    return added


def people_list():
    rows = store.db().execute("select name, count(*) from people group by name order by name").fetchall()
    return [{"name": n, "references": k} for n, k in rows]


def forget_person(name):
    with store.db() as c:
        return c.execute("delete from people where name=?", (name,)).rowcount


# ── tools the model calls ─────────────────────────────────────────────────

def t_list_videos():
    vs = store.videos()
    if not vs:
        return "No videos yet. The user can attach one to the conversation.", []
    return "\n".join(f"{v['id']}: {v['name']} ({_fmt(v['duration'] or 0)})" for v in vs), []


def t_people():
    ps = people_list()
    if not ps:
        return "Nobody is enrolled. find_person can only find enrolled people.", []
    return "Enrolled: " + ", ".join(f"{p['name']} ({p['references']} photos)" for p in ps), []


def t_overview(video, max_frames=12):
    v = _need(video)
    ensure_scenes(video)
    sc = store.db().execute("select start, end from scenes where video=? order by idx", (video,)).fetchall()
    max_frames = max(1, min(int(max_frames), 16))
    pick = sc if len(sc) <= max_frames else [sc[round(i * (len(sc) - 1) / (max_frames - 1))] for i in range(max_frames)]
    text = f"{v['name']}: {_fmt(v['duration'])}, {len(sc)} scenes. Showing one frame from {len(pick)} of them."
    att = []
    for s, e in pick:
        t = (s + e) / 2
        att += [{"type": "text", "text": f"Scene {_fmt(s)}-{_fmt(e)}, frame at {_fmt(t)}:"}, _img(media.frame_jpeg(v["path"], t))]
    return text, att


def t_look(video, times):
    v = _need(video)
    times = [float(t) for t in times][:8]
    att = []
    for t in times:
        t = min(max(0.0, t), max(0.0, v["duration"] - 0.05))
        att += [{"type": "text", "text": f"Frame at {_fmt(t)}:"}, _img(media.frame_jpeg(v["path"], t, max_side=960))]
    return f"Showing {len(times)} frame(s) from {v['name']}.", att


def t_watch(video, start, end):
    v = _need(video)
    start = max(0.0, float(start))
    end = min(float(end), v["duration"], start + MAX_WATCH)
    if end <= start:
        return "Empty range.", []
    mp4 = media.clip_mp4(v["path"], start, end)
    url = "data:video/mp4;base64," + base64.b64encode(mp4).decode()
    note = "" if float(end) - start < MAX_WATCH else f" (capped at {int(MAX_WATCH)} s; watch further ranges separately)"
    return f"Showing {v['name']} {_fmt(start)}-{_fmt(end)} as video{note}. Times in it are relative to {_fmt(start)}.", [
        {"type": "video_url", "video_url": {"url": url}}]


def t_listen(video, start=None, end=None):
    v = _need(video)
    ensure_transcript(video)
    meta = store.db().execute("select text from transcript where video=? and start < 0", (video,)).fetchone()
    lang, prob = (meta[0].split()[1], float(meta[0].split()[2])) if meta else ("?", 0.0)
    q = "select start, end, text from transcript where video=? and start >= 0"
    args = [video]
    if start is not None:
        q += " and end >= ?"; args.append(float(start))
    if end is not None:
        q += " and start <= ?"; args.append(float(end))
    rows = store.db().execute(q + " order by start", args).fetchall()
    if not rows:
        return f"No clear speech found in {v['name']}" + (" in that range" if start is not None else "") + \
            " (low-confidence segments are dropped; music, singing and noise often look like this).", []
    head = f"Speech in {v['name']} (language {lang}, confidence {prob:.2f}"
    head += "; LOW: treat with caution)" if prob < 0.5 else ")"
    return head + ":\n" + "\n".join(f"[{_fmt(s)}-{_fmt(e)}] {t}" for s, e, t in rows)[:6000], []


def t_find_person(name, video=None):
    gallery = [store.vec(b) for (b,) in store.db().execute("select emb from people where name=?", (name,))]
    if not gallery:
        return f"{name} is not enrolled, so they cannot be found. Enrolled: " + (
            ", ".join(p["name"] for p in people_list()) or "nobody"), []
    g = np.stack(gallery)
    vids = [v["id"] for v in store.videos()] if _every(video) else [video]
    out, att = [], []
    for vid in vids:
        v = _need(vid)
        ensure_faces(vid)
        rows = store.db().execute("select t, emb from faces where video=? order by t", (vid,)).fetchall()
        hits, best_miss = [], 0.0
        for t, b in rows:
            sim = float((g @ store.vec(b)).max())
            if sim >= FACE_THRESHOLD:
                hits.append((t, sim))
            else:
                best_miss = max(best_miss, sim)
        if not hits:
            out.append(f"{v['name']} ({vid}): {name} NOT FOUND among {len(rows)} detected faces "
                       f"(closest similarity {best_miss:.2f}, threshold {FACE_THRESHOLD}).")
            continue
        segs, cur = [], [hits[0][0], hits[0][0], hits[0][1]]
        for t, s in hits[1:]:
            if t - cur[1] <= 2 * FACE_STEP:
                cur[1], cur[2] = t, max(cur[2], s)
            else:
                segs.append(cur); cur = [t, t, s]
        segs.append(cur)
        out.append(f"{v['name']} ({vid}): {name} appears in {len(segs)} segment(s): " + "; ".join(
            f"{_fmt(a)}-{_fmt(b + FACE_STEP)} (similarity {s:.2f})" for a, b, s in segs))
        # One frame per segment, so the model sees every appearance without
        # having to ask (GLM-9B tended to look at the first segment only).
        for a, b, _ in segs[:6]:
            t = (a + b) / 2
            att += [{"type": "text", "text": f"{name} in {v['name']} at {_fmt(t)}:"}, _img(media.frame_jpeg(v["path"], t))]
    return "\n".join(out), att


def t_search(query, video=None, top=6):
    import torch
    model, proc = models.siglip()
    with torch.no_grad():
        inp = proc(text=[query], padding="max_length", max_length=64, return_tensors="pt").to("cuda")
        q = torch.nn.functional.normalize(model.get_text_features(**inp).float(), dim=-1).cpu().numpy()[0]
    if _every(video):
        video = None
    vids = [video] if video else [v["id"] for v in store.videos()]
    scored = []
    for vid in vids:
        ensure_frames(vid)
        for t, b in store.db().execute("select t, emb from frames where video=?", (vid,)):
            scored.append((float(store.vec(b) @ q), vid, t))
    scored.sort(reverse=True)
    picked, seen = [], []
    for s, vid, t in scored:
        if all(not (vid == pv and abs(t - pt) < 6) for pv, pt in seen):
            picked.append((s, vid, t)); seen.append((vid, t))
        if len(picked) >= int(top):
            break
    if not picked:
        return "Nothing indexed to search.", []
    lines = [f"{vid} at {_fmt(t)} (score {s:.3f})" for s, vid, t in picked]
    att = []
    for s_, vid, t in picked[:4]:
        v = store.video(vid)
        att += [{"type": "text", "text": f"Match in {v['name']} ({vid}) at {_fmt(t)}:"}, _img(media.frame_jpeg(v["path"], t))]
    return ("Best matches for '" + query + "' across " + (f"{video}" if video else "ALL videos") +
            " (higher is better; the frames are attached):\n" + "\n".join(lines)), att


TOOLS = {
    "list_videos": t_list_videos,
    "people": t_people,
    "overview": t_overview,
    "look": t_look,
    "watch": t_watch,
    "listen": t_listen,
    "find_person": t_find_person,
    "search": t_search,
}


def _fn(name, desc, props=None, required=()):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {}, "required": list(required)}}}


V = {"type": "string", "description": "video id, e.g. v_1a2b3c4d5e6f"}
SPEC = [
    _fn("list_videos", "List the videos available, with ids and durations."),
    _fn("people", "List people enrolled for face recognition."),
    _fn("overview", "Get the scene list of a video and one frame per scene. Use first to understand what a video is about.",
        {"video": V, "max_frames": {"type": "integer", "description": "1-16, default 12"}}, ["video"]),
    _fn("look", "See sharp frames at specific times (seconds). Best for details: faces, text, objects.",
        {"video": V, "times": {"type": "array", "items": {"type": "number"}, "description": "up to 8 timestamps in seconds"}},
        ["video", "times"]),
    _fn("watch", "Watch a range of a video as moving video (max 90 s). Best for motion, actions and the order of events.",
        {"video": V, "start": {"type": "number"}, "end": {"type": "number"}}, ["video", "start", "end"]),
    _fn("listen", "Get the speech transcript of a video, optionally only between start and end seconds.",
        {"video": V, "start": {"type": "number"}, "end": {"type": "number"}}, ["video"]),
    _fn("find_person", "Find when an ENROLLED person appears, using face recognition. The ONLY reliable way to "
        "decide whether someone is in a video; it can answer NOT FOUND. Omit video to search all videos.",
        {"name": {"type": "string"}, "video": V}, ["name"]),
    _fn("search", "Find the moments that best match a text description (e.g. 'kids opening presents'), with "
        "frames attached. To answer 'which video...', call it WITHOUT video so it searches every video.",
        {"query": {"type": "string"}, "video": V}, ["query"]),
]
