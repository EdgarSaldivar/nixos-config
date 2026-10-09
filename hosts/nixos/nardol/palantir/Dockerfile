# Palantír's runtime: the GPU libraries for its tools, nothing of its own code.
# The app is mounted read-only from the Nix store (see ../palantir.nix), so a
# code change is a normal deploy and this image only changes with its deps.
#
# Build on nardol, then pin the printed ID in ../palantir.nix (`image`):
#   sudo docker build -t palantir-runtime:1 hosts/nixos/nardol/palantir
#   sudo docker image inspect palantir-runtime:1 --format '{{.Id}}'
#
# Base: PyTorch 2.5.1, CUDA 12.4, cuDNN 9 — what ctranslate2 (faster-whisper)
# and onnxruntime-gpu (InsightFace) both need. Verified on the 4090 2026-10-08.
FROM pytorch/pytorch@sha256:c8268a92a69bd500f8be0e665b2630ee006dadaf7bfbc24249141b15ff622755

ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
# ctranslate2 (faster-whisper) looks for cuBLAS/cuDNN on the library path, but
# this base ships them inside PyTorch's nvidia-* wheels. Without this, Whisper
# fails at first use with "libcublas.so.12 is not found" (2026-10-08).
ENV LD_LIBRARY_PATH=/opt/conda/lib/python3.11/site-packages/nvidia/cublas/lib:/opt/conda/lib/python3.11/site-packages/nvidia/cudnn/lib
# ffmpeg for decoding and clipping; a compiler because insightface ships as an
# sdist with a Cython extension.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg build-essential \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt \
 && pip freeze > /requirements.lock \
 && apt-get purge -y build-essential && apt-get autoremove -y
