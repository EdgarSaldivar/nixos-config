# Build on nardol, then pin the printed ID in lib/inference-profiles.nix:
#   sudo docker build -t vllm-openai:0.30.0-embedq hosts/nixos/nardol/vllm-embedq
#   sudo docker image inspect vllm-openai:0.30.0-embedq --format "{{.Id}}"
# A rebuild produces a NEW ID even from this same file; update the profile.
#
# vLLM 0.30.0 (the pinned digest) + quantized token embeddings for Qwen3.5/3.8.
# The CompressedTensorsEmbeddingWNA16 kernel ships upstream; qwen3_5.py and
# qwen3_5_mtp.py just never pass quant_config to VocabParallelEmbedding, so a
# checkpoint with INT4/INT8 embeddings fails to load. Same change as
# JRamirez-UAB/Qwen3.8-27B-GPTQ-W4A16-embed-int4-24GB patches/qwen3_5-embed-quant.patch.
FROM vllm/vllm-openai@sha256:5f5e535216848d0c52159c8c13a0af04be5f6fe1a84e79914300610796f76d40
RUN python3 - <<"PY"
import re, pathlib
root = pathlib.Path("/usr/local/lib/python3.12/dist-packages/vllm/model_executor/models")
for name, qc in (("qwen3_5.py", "self.quant_config"), ("qwen3_5_mtp.py", "vllm_config.quant_config")):
    p = root / name
    s = p.read_text()
    old = "self.embed_tokens = VocabParallelEmbedding(\n            self.vocab_size,\n            config.hidden_size,\n        )"
    new = ("self.embed_tokens = VocabParallelEmbedding(\n            self.vocab_size,\n            config.hidden_size,\n"
           f"            quant_config={qc},\n            prefix=maybe_prefix(prefix, \"embed_tokens\"),\n        )")
    assert s.count(old) == 1, name
    assert "maybe_prefix" in s, name
    p.write_text(s.replace(old, new))
print("patched")
PY
