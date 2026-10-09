"""Video decoding, clipping and ingest, all through ffmpeg."""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.request

import numpy as np

from . import store

VIDEOS = os.path.join(store.DATA, "videos")
# Local paths a request may name. The inbox always; library roots when a
# library is mounted (PALANTIR_LIBRARY, colon-separated). Nothing else on the
# host is readable through the API.
ROOTS = [os.path.join(store.DATA, "inbox")] + [
    p for p in os.environ.get("PALANTIR_LIBRARY", "").split(":") if p
]


def _run(args, **kw):
    return subprocess.run(args, check=True, capture_output=True, **kw)


def probe(path):
    out = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path]).stdout
    info = json.loads(out)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    has_audio = any(s.get("codec_type") == "audio" for s in info["streams"])
    return {
        "duration": float(info["format"].get("duration") or 0),
        "width": v.get("width"),
        "height": v.get("height"),
        "fps": fps,
        "audio": has_audio,
    }


def _hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _allowed(path):
    real = os.path.realpath(path)
    return any(real == r or real.startswith(os.path.realpath(r) + os.sep) for r in ROOTS)


def ingest(source, name=None):
    """Register a video from a URL, a data: URL, or an allowed local path.

    The file is copied into the data volume under its content hash, so the
    index never depends on where it came from.
    """
    os.makedirs(VIDEOS, exist_ok=True)
    tmp = os.path.join(VIDEOS, f".incoming-{time.time_ns()}")
    if source.startswith("data:"):
        header, _, payload = source.partition(",")
        with open(tmp, "wb") as f:
            f.write(base64.b64decode(payload))
        name = name or "upload"
    elif source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=600) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        name = name or os.path.basename(source.split("?")[0])
    else:
        path = source[len("file://"):] if source.startswith("file://") else source
        if not _allowed(path):
            raise ValueError(f"{path} is outside the allowed roots {ROOTS}")
        shutil.copyfile(path, tmp)
        name = name or os.path.basename(path)
    vid = "v_" + _hash(tmp)[:12]
    existing = store.video(vid)
    if existing:
        os.unlink(tmp)
        return existing
    ext = os.path.splitext(name)[1] or ".mp4"
    final = os.path.join(VIDEOS, vid + ext)
    os.replace(tmp, final)
    info = probe(final)
    with store.db() as c:
        c.execute(
            "insert into videos values(?,?,?,?,?,?,?,?)",
            (vid, final, name, info["duration"], info["width"], info["height"], info["fps"], time.time()),
        )
    return store.video(vid)


def frame_jpeg(path, t, max_side=640):
    """One frame at t seconds as JPEG bytes, longest side max_side."""
    vf = f"scale='if(gt(iw,ih),min({max_side},iw),-2)':'if(gt(iw,ih),-2,min({max_side},ih))'"
    return _run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                 "-vf", vf, "-q:v", "3", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]).stdout


def frames_rgb(path, step=1.0, max_side=960):
    """Yield (t, HxWx3 uint8 RGB) every `step` seconds, decoded in one pass."""
    info = probe(path)
    w, h = info["width"], info["height"]
    scale = min(1.0, max_side / max(w, h))
    ow, oh = int(w * scale) // 2 * 2, int(h * scale) // 2 * 2
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", path, "-vf", f"fps=1/{step},scale={ow}:{oh}",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        stdout=subprocess.PIPE)
    size = ow * oh * 3
    i = 0
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield i * step, np.frombuffer(buf, np.uint8).reshape(oh, ow, 3)
            i += 1
    finally:
        proc.stdout.close()
        proc.wait()


def clip_mp4(path, start, end, max_side=640):
    """A re-encoded clip [start, end] as MP4 bytes, for the model's video input."""
    vf = f"scale='if(gt(iw,ih),min({max_side},iw),-2)':'if(gt(iw,ih),-2,min({max_side},ih))'"
    out = os.path.join(VIDEOS, f".clip-{time.time_ns()}.mp4")
    try:
        _run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", path,
              "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
              "-movflags", "+faststart", out])
        with open(out, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(out):
            os.unlink(out)


def audio_wav(path):
    """The soundtrack as 16 kHz mono float32, or None if there is none."""
    if not probe(path)["audio"]:
        return None
    raw = _run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", "16000",
                "-f", "f32le", "pipe:1"]).stdout
    return np.frombuffer(raw, np.float32)


def scenes(path):
    """Shot boundaries via PySceneDetect's AdaptiveDetector."""
    from scenedetect import AdaptiveDetector, detect
    found = detect(path, AdaptiveDetector(), show_progress=False)
    dur = probe(path)["duration"]
    if not found:
        return [(0.0, dur)]
    return [(s.get_seconds(), e.get_seconds()) for s, e in found]
