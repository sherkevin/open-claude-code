#!/usr/bin/env python3
"""Install/verify the pinned local knowledge embedding weights; no documents uploaded."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

REPOSITORY = "Qdrant/bge-small-zh-v1.5"
REVISION = "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"
MODEL_SHA256 = "1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38"
FILES = ["config.json", "model_optimized.onnx", "special_tokens_map.json", "tokenizer_config.json", "tokenizer.json"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--download", action="store_true", help="Allow downloading the public pinned model weights")
    parser.add_argument("--model-path", type=Path, help="Verify an already copied offline model directory")
    args = parser.parse_args()
    from fastembed import TextEmbedding
    if args.model_path:
        path = args.model_path.resolve()
    else:
        from huggingface_hub import snapshot_download
        path = Path(snapshot_download(repo_id=REPOSITORY, revision=REVISION, cache_dir=str(args.cache_dir.resolve()),
                                      allow_patterns=FILES, local_files_only=not args.download))
    missing = [name for name in FILES if not (path / name).is_file()]
    if missing:
        raise SystemExit("Missing model files: " + ", ".join(missing))
    digest = hashlib.sha256((path / "model_optimized.onnx").read_bytes()).hexdigest()
    if digest != MODEL_SHA256:
        raise SystemExit("Pinned embedding model SHA256 does not match")
    model = TextEmbedding(model_name="BAAI/bge-small-zh-v1.5", specific_model_path=str(path), local_files_only=True, threads=2)
    vector = next(model.embed(["工业设备运行记录与质量检验"])).tolist()
    if len(vector) != 512:
        raise SystemExit("Unexpected model vector dimension")
    print(json.dumps({"model": "BAAI/bge-small-zh-v1.5", "revision": REVISION, "sha256": digest, "dimensions": len(vector),
                      "environment": {"AGENT_KNOWLEDGE_EMBEDDING_MODEL": "BAAI/bge-small-zh-v1.5", "AGENT_KNOWLEDGE_MODEL_PATH": str(path),
                                      "AGENT_KNOWLEDGE_MODEL_CACHE": str(args.cache_dir.resolve()), "AGENT_KNOWLEDGE_ALLOW_MODEL_DOWNLOAD": "false"}}, indent=2))


if __name__ == "__main__":
    main()
