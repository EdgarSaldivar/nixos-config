"""The tool models, loaded on first use and dropped after idling.

They share the 4090 with GLM (which is held to 0.65 of it), so none is loaded
until a tool needs it, and each is released after IDLE seconds unused.
"""
import os
import threading
import time

MODELS = os.path.join(os.environ.get("PALANTIR_DATA", "/data"), "models")
IDLE = int(os.environ.get("PALANTIR_MODEL_IDLE", "600"))
WHISPER = os.environ.get("PALANTIR_WHISPER", "large-v3-turbo")
SIGLIP = os.environ.get("PALANTIR_SIGLIP", "google/siglip2-so400m-patch14-384")

_lock = threading.Lock()
_loaded = {}  # name -> (object, last_used)


def _get(name, loader):
    with _lock:
        if name not in _loaded:
            _loaded[name] = [loader(), time.time()]
        _loaded[name][1] = time.time()
        return _loaded[name][0]


def _reaper():
    while True:
        time.sleep(30)
        with _lock:
            for name in [n for n, (_, t) in _loaded.items() if time.time() - t > IDLE]:
                del _loaded[name]
                try:
                    import torch
                    torch.cuda.empty_cache()
                except Exception:
                    pass


threading.Thread(target=_reaper, daemon=True).start()


def loaded():
    with _lock:
        return sorted(_loaded)


def whisper():
    def load():
        from faster_whisper import WhisperModel
        return WhisperModel(WHISPER, device="cuda", compute_type="int8_float16",
                            download_root=os.path.join(MODELS, "whisper"))
    return _get("whisper", load)


def faces():
    def load():
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(name="buffalo_l", root=os.path.join(MODELS, "insightface"),
                           providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
                           allowed_modules=["detection", "recognition"])
        app.prepare(ctx_id=0, det_size=(640, 640))
        return app
    return _get("faces", load)


def siglip():
    def load():
        import torch
        from transformers import AutoModel, AutoProcessor
        os.environ.setdefault("HF_HOME", os.path.join(MODELS, "hf"))
        model = AutoModel.from_pretrained(SIGLIP, torch_dtype=torch.float16).to("cuda").eval()
        return model, AutoProcessor.from_pretrained(SIGLIP)
    return _get("siglip", load)
