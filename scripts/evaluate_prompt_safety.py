#!/usr/bin/env python3
"""Compare the shipped rules and Qwen on the committed synthetic Chinese cases."""

import argparse
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from app.config import Settings
from app.prompt_safety import PromptPolicy, PromptSafety, ReviewFailure, parse_qwen_decision
from app.security import AppError


def local_model(model_dir):
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY"):
        os.environ[key] = "1"
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    torch.set_num_threads(4)
    tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(model_dir, local_files_only=True, dtype=torch.float32).eval()

    def classify(text):
        inputs = tokenizer.apply_chat_template([{"role": "user", "content": text}],
                                               return_tensors="pt", return_dict=True)
        with torch.inference_mode():
            output = model.generate(**inputs, do_sample=False, max_new_tokens=128)
        generated = output[0][inputs.input_ids.shape[-1]:]
        if generated[-1].item() not in model.generation_config.eos_token_id:
            raise ReviewFailure("response")
        return parse_qwen_decision(tokenizer.decode(generated, skip_special_tokens=True))

    return classify


def elapsed_ms(start):
    return round((time.monotonic() - start) * 1000, 1)


def measure(args):
    policy_path = ROOT / "server/prompt-safety.example.json"
    policy = PromptPolicy.model_validate_json(policy_path.read_bytes())
    rules = PromptSafety(Settings(prompt_safety_mode="rules", prompt_safety_policy=str(policy_path)))
    cases = json.loads((ROOT / "fixtures/prompt-safety/zh-CN.json").read_text(encoding="utf-8"))
    classifier_start = time.monotonic()
    if args.url:
        classify = PromptSafety(Settings(prompt_safety_mode="qwen", prompt_safety_qwen_url=args.url,
                                          prompt_safety_timeout_seconds="120")).classify
    else:
        classify = local_model(args.model_dir)
    classify("请整理一份会议纪要。")
    warmup_ms = elapsed_ms(classifier_start)
    rows = []
    for case in cases:
        row = {key: case[key] for key in ("id", "group", "decision")}
        start = time.monotonic()
        try:
            rules.require_allowed(case["text"])
            row["rules"] = "allow"
        except AppError as exc:
            if exc.code != "PROMPT_BLOCKED":
                raise
            row["rules"] = "block"
        row["rules_ms"] = elapsed_ms(start)
        start = time.monotonic()
        try:
            level, categories = classify(case["text"])
            row.update(qwen="block" if policy.blocked_qwen_categories(level, categories) else "allow",
                       level=level, categories=sorted(categories))
        except ReviewFailure as exc:
            row.update(qwen="error", error=str(exc))
        row["qwen_ms"] = elapsed_ms(start)
        row["combined"] = "block" if row["rules"] == "block" else row["qwen"]
        rows.append(row)

    stats = {arm: {
        "correct": sum(row["decision"] == row[arm] for row in rows),
        "misses": sum(row["decision"] == "block" and row[arm] == "allow" for row in rows),
        "false_blocks": sum(row["decision"] == "allow" and row[arm] == "block" for row in rows),
        "errors": sum(row[arm] == "error" for row in rows),
    } for arm in ("rules", "qwen", "combined")}
    latencies = {}
    for arm in ("rules", "qwen"):
        times = sorted(row[arm + "_ms"] for row in rows)
        latencies[arm] = {"median": statistics.median(times), "p95": times[math.ceil(0.95 * len(times)) - 1]}
    return {"cases": len(rows), "decisions": stats, "latency_ms": latencies,
            "warmup_ms": warmup_ms, "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--model-dir", type=Path, default=ROOT / ".venv/qwen3guard")
    source.add_argument("--url", help="Use the native Chat Completions API on a loopback address.")
    args = parser.parse_args()
    result = measure(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["decisions"]["qwen"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
