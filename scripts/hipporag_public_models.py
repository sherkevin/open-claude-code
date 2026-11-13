"""Explicit real-model factories for the public-source HippoRAG pilot only.

Importing this module performs no inference or downloads. A trusted host must
opt its process adapter into network access and provide private budget/trace
paths. No credentials are read; the fixed loopback gateway owns authentication.
Evaluation annotations must never be supplied to these factories or prompts.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
import urllib.error
import urllib.request
import uuid


MODEL = "tencent-cloud/deepseek-v4.1-flash-1017"
ENDPOINT = "http://127.0.0.1:3425/v1/chat/completions"
EMBEDDING_REPO = "intfloat/multilingual-e5-small"
EMBEDDING_REVISION = "fd1525a9fd15316a2d503bf26ab031a61d056e98"
EMBEDDING_FILES = frozenset({"config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json",
                            "special_tokens_map.json", "sentencepiece.bpe.model", "README.md"})
MAX_CALLS = 64
MAX_TOKENS = 2048
THINKING_MODE = "disabled"
TIMEOUT_SECONDS = 60
MAX_RESPONSE_BYTES = 1_000_000
_TOKEN_KEYS = ("max_new_tokens", "max_completion_tokens", "max_output_tokens", "max_tokens")
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")
_ERRORS = {"invalid_input", "budget_exhausted", "audit_unavailable", "model_timeout", "model_http_error",
           "model_transport_error", "model_protocol_error", "model_truncated", "model_circuit_open", "weights_untrusted"}


class PublicModelError(RuntimeError):
    def __init__(self, code):
        if code not in _ERRORS:
            raise ValueError("Unknown public pilot model error")
        self.code = code
        super().__init__("Public pilot model operation failed: " + code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _parse_json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError()

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)


def _private_path(value):
    path = Path(value).absolute()
    if ".." in path.parts or any(parent.is_symlink() or not parent.is_dir() for parent in path.parents):
        raise ValueError()
    return path


@contextmanager
def _private_file(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "r+b") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid() or info.st_nlink != 1):
            raise ValueError()
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield stream
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class CallLedger:
    """The host shares one budget_path across every arm and worker of a run.

    Reserve before dispatch; failures consume a call too. A crash-corrupted
    budget fails closed rather than resetting the allowance. This is a trusted
    host ledger, not protection against a same-UID actor replacing its files.
    """

    def __init__(self, *, trace_path, budget_path):
        try:
            self.trace_path, self.budget_path = _private_path(trace_path), _private_path(budget_path)
            if self.trace_path == self.budget_path:
                raise ValueError()
        except Exception:
            raise PublicModelError("audit_unavailable") from None

    def reserve(self):
        try:
            with _private_file(self.budget_path) as stream:
                raw = stream.read(4097)
                if len(raw) > 4096:
                    raise ValueError()
                value = _parse_json(raw) if raw else {"schema": "hipporag-public-budget/v1", "limit": MAX_CALLS, "used": 0}
                if (type(value) is not dict or set(value) != {"schema", "limit", "used"}
                        or value["schema"] != "hipporag-public-budget/v1" or type(value["limit"]) is not int
                        or value["limit"] != MAX_CALLS or type(value["used"]) is not int
                        or not 0 <= value["used"] <= MAX_CALLS):
                    raise ValueError()
                if value["used"] == MAX_CALLS:
                    raise PublicModelError("budget_exhausted")
                value["used"] += 1
                stream.seek(0)
                stream.write(_json(value))
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
                return value["used"]
        except PublicModelError:
            raise
        except Exception:
            raise PublicModelError("audit_unavailable") from None

    def record(self, value):
        try:
            encoded = _json(value) + b"\n"
            if len(encoded) > 8192:
                raise ValueError()
            with _private_file(self.trace_path) as stream:
                stream.seek(0, 2)
                if stream.tell() + len(encoded) > 2_000_000:
                    raise ValueError()
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            raise PublicModelError("audit_unavailable") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PublicModelError("model_http_error")


def _usage(response):
    value = response.get("usage")
    if value is None:
        return None, "missing"
    if type(value) is not dict:
        raise PublicModelError("model_protocol_error")
    measured = {}
    for key in _USAGE_KEYS:
        if key not in value or value[key] is None:
            continue
        if type(value[key]) is not int or not 0 <= value[key] <= 10_000_000:
            raise PublicModelError("model_protocol_error")
        measured[key] = value[key]
    return (measured or None), "reported" if len(measured) == len(_USAGE_KEYS) else "partial" if measured else "missing"


class LoopbackLLM:
    """One bounded HTTP attempt; any failure permanently opens this instance.

    No retries, fallback, or automatic circuit reset. Serializing infer also
    prevents a queued upstream extraction call from dispatching after failure.
    """

    def __init__(self, *, trace_path, budget_path):
        self.ledger = CallLedger(trace_path=trace_path, budget_path=budget_path)
        self._lock = threading.Lock()
        self._circuit_reason = None

    def infer(self, messages, **kwargs):
        with self._lock:
            if self._circuit_reason is not None:
                prompt_digest = None
                try:
                    prompt = _json(messages)
                    if len(prompt) <= 512_000:
                        prompt_digest = hashlib.sha256(prompt).hexdigest()
                except Exception:
                    pass
                self.ledger.record({"event": "llm_denied", "status": "error", "call_id": uuid.uuid4().hex,
                                    "model": MODEL, "thinking_mode": THINKING_MODE, "prompt_sha256": prompt_digest,
                                    "error_code": "model_circuit_open", "first_error_code": self._circuit_reason,
                                    "usage": None, "usage_status": "missing"})
                raise PublicModelError("model_circuit_open")
            try:
                return self._infer(messages, **kwargs)
            except Exception as exc:
                self._circuit_reason = exc.code if isinstance(exc, PublicModelError) else "model_protocol_error"
                if isinstance(exc, PublicModelError):
                    raise
                raise PublicModelError("model_protocol_error") from None

    def _infer(self, messages, **kwargs):
        try:
            if (type(messages) is not list or not 1 <= len(messages) <= 64
                    or any(type(item) is not dict or set(item) != {"role", "content"}
                           or item["role"] not in {"system", "user", "assistant"}
                           or type(item["content"]) is not str or not item["content"].strip() for item in messages)):
                raise ValueError()
            aliases = [key for key in _TOKEN_KEYS if key in kwargs]
            if len(aliases) > 1 or set(kwargs) - {*_TOKEN_KEYS, "response_format"}:
                raise ValueError()
            tokens = kwargs[aliases[0]] if aliases else MAX_TOKENS
            if type(tokens) is not int or not 1 <= tokens <= MAX_TOKENS:
                raise ValueError()
            # TokenHub defaults to thinking enabled; extraction needs its output
            # budget for the requested JSON rather than hidden reasoning.
            # https://cloud.tencent.com/document/product/1823/132248
            payload = {"model": MODEL, "messages": messages, "max_tokens": tokens, "temperature": 0, "stream": False,
                       "thinking": {"type": THINKING_MODE}}
            if "response_format" in kwargs:
                if kwargs["response_format"] not in ({"type": "json_object"}, {"type": "text"}):
                    raise ValueError()
                payload["response_format"] = kwargs["response_format"]
            prompt = _json(messages)
            if len(prompt) > 512_000:
                raise ValueError()
            encoded = _json(payload)
        except Exception:
            raise PublicModelError("invalid_input") from None

        call_id = uuid.uuid4().hex
        common = {"call_id": call_id, "model": MODEL, "thinking_mode": THINKING_MODE,
                  "prompt_sha256": hashlib.sha256(prompt).hexdigest()}
        try:
            common["sequence"] = self.ledger.reserve()
        except PublicModelError as exc:
            self.ledger.record({**common, "event": "llm_denied", "status": "error", "error_code": exc.code,
                                "usage": None, "usage_status": "missing"})
            raise
        self.ledger.record({**common, "event": "llm_attempt", "max_tokens": tokens,
                            "input_bytes": len(prompt), "timeout_seconds": TIMEOUT_SECONDS})
        started = time.monotonic()
        usage, usage_status, returned_model, finish_reason = None, "missing", None, None
        try:
            request = urllib.request.Request(ENDPOINT, data=encoded, headers={"Content-Type": "application/json"}, method="POST")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
            with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                if response.status != 200:
                    raise PublicModelError("model_http_error")
                body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise PublicModelError("model_protocol_error")
            try:
                result = _parse_json(body)
                if type(result) is not dict:
                    raise ValueError()
                usage, usage_status = _usage(result)
                if usage_status == "reported" and usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
                    raise ValueError()
                returned_model = result.get("model")
                if returned_model is not None and (type(returned_model) is not str
                                                   or not re.fullmatch(r"[A-Za-z0-9_./:@-]{1,200}", returned_model)):
                    returned_model = None
                    raise ValueError()
                choices = result["choices"]
                if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
                    raise ValueError()
                choice = choices[0]
                message, finish_reason = choice["message"], choice.get("finish_reason")
                if type(finish_reason) is not str or finish_reason not in {"stop", "length"}:
                    finish_reason = None
                    raise ValueError()
                if (type(message) is not dict or message.get("role") != "assistant"
                        or type(message.get("content")) is not str or not message["content"].strip()
                        or message.get("tool_calls") or message.get("refusal")):
                    raise ValueError()
                content = message["content"]
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise PublicModelError("model_protocol_error") from None
            if finish_reason == "length":
                raise PublicModelError("model_truncated")
        except Exception as exc:
            timed_out = isinstance(exc, TimeoutError) or isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)
            code = (exc.code if isinstance(exc, PublicModelError) else "model_timeout" if timed_out
                    else "model_http_error" if isinstance(exc, urllib.error.HTTPError) else "model_transport_error")
            self.ledger.record({**common, "event": "llm_result", "status": "error", "error_code": code,
                                "usage": usage, "usage_status": usage_status, "returned_model": returned_model,
                                "finish_reason": finish_reason, "elapsed_seconds": round(time.monotonic() - started, 3)})
            raise PublicModelError(code) from None
        self.ledger.record({**common, "event": "llm_result", "status": "ok", "usage": usage, "usage_status": usage_status,
                            "returned_model": returned_model, "finish_reason": finish_reason,
                            "elapsed_seconds": round(time.monotonic() - started, 3)})
        metadata = {"model": MODEL, "thinking_mode": THINKING_MODE, "returned_model": returned_model, "finish_reason": finish_reason,
                    "usage": usage, "usage_status": usage_status}
        if usage is not None:
            metadata.update(usage)
        return content, metadata, False


def verify_weights(weights_dir):
    """Validate the host's pinned download receipt before loading local tensors.

    The receipt is provenance from the trusted downloader, not a signature.
    Only the seven declared files and revision.json are accepted.
    """
    try:
        directory = _private_path(weights_dir)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError()
        if {item.name for item in directory.iterdir()} != EMBEDDING_FILES | {"revision.json"}:
            raise ValueError()
        receipt_path = directory / "revision.json"
        receipt_info = receipt_path.lstat()
        if not stat.S_ISREG(receipt_info.st_mode) or receipt_info.st_nlink != 1 or receipt_info.st_size > 8192:
            raise ValueError()
        receipt = _parse_json(receipt_path.read_bytes())
        if (type(receipt) is not dict or set(receipt) != {"repo", "revision", "files"}
                or receipt["repo"] != EMBEDDING_REPO or receipt["revision"] != EMBEDDING_REVISION
                or type(receipt["files"]) is not dict or set(receipt["files"]) != EMBEDDING_FILES):
            raise ValueError()
        for name, expected in receipt["files"].items():
            path = directory / name
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 600_000_000
                    or type(expected) is not str or not re.fullmatch(r"[0-9a-f]{64}", expected)):
                raise ValueError()
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(65536), b""):
                    digest.update(block)
            if digest.hexdigest() != expected:
                raise ValueError()
        return directory
    except Exception:
        raise PublicModelError("weights_untrusted") from None


class LocalE5Embedding:
    query_instruction_mode = "shared"
    embedding_dim = 384

    def __init__(self, weights_dir):
        directory = verify_weights(weights_dir)
        import torch
        from transformers import AutoModel, AutoTokenizer

        self._torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(str(directory), local_files_only=True, trust_remote_code=False)
        self.model = AutoModel.from_pretrained(str(directory), local_files_only=True, trust_remote_code=False,
                                             use_safetensors=True).to("cpu").eval()
        if self.model.config.hidden_size != self.embedding_dim:
            raise PublicModelError("weights_untrusted")

    def batch_encode(self, texts, *, instruction=None, norm=True, query=False):
        import numpy as np

        if type(texts) in (str, np.str_):
            texts = [texts]
        try:
            if (not isinstance(texts, (list, tuple)) or len(texts) > 5000 or type(query) is not bool or norm is not True
                    or instruction is not None and type(instruction) is not str
                    or any(type(text) not in (str, np.str_) for text in texts)):
                raise ValueError()
            # Official extract_entity_nodes returns NumPy strings via np.unique.
            texts = [str(text) if type(text) is np.str_ else text for text in texts]
            if any(not text.strip() or len(text.encode("utf-8")) > 65536 for text in texts):
                raise ValueError()
            if sum(len(text.encode("utf-8")) for text in texts) > 16_000_000:
                raise ValueError()
        except Exception:
            raise PublicModelError("invalid_input") from None
        prefix = "query: " if query or instruction else "passage: "
        encoded = []
        with self._torch.inference_mode():
            for offset in range(0, len(texts), 32):
                batch = self.tokenizer([prefix + text for text in texts[offset:offset + 32]],
                                       max_length=512, padding=True, truncation=True, return_tensors="pt")
                output = self.model(**batch)
                mask = batch["attention_mask"].unsqueeze(-1).bool()
                pooled = output.last_hidden_state.masked_fill(~mask, 0.0).sum(dim=1) / mask.sum(dim=1).clamp(min=1)
                normalized = self._torch.nn.functional.normalize(pooled, p=2, dim=1).cpu().numpy().astype(np.float32)
                if (normalized.shape != (len(texts[offset:offset + 32]), self.embedding_dim)
                        or not np.isfinite(normalized).all() or not np.allclose(np.linalg.norm(normalized, axis=1), 1, atol=1e-5)):
                    raise PublicModelError("model_protocol_error")
                encoded.append(normalized)
        return np.concatenate(encoded, axis=0) if encoded else np.empty((0, self.embedding_dim), dtype=np.float32)


def make_engine(*, save_dir, weights_dir, trace_path, budget_path):
    from hipporag import HippoRAG
    from hipporag.utils.config_utils import BaseConfig

    llm = LoopbackLLM(trace_path=trace_path, budget_path=budget_path)
    embedding = LocalE5Embedding(weights_dir)
    return HippoRAG(
        global_config=BaseConfig(save_dir=str(save_dir), llm_name=MODEL, embedding_model_name=EMBEDDING_REPO,
                                 llm_base_url="http://127.0.0.1:3425/v1", embedding_max_seq_len=512,
                                 openie_mode="online", openie_max_workers=1, synonymy_edge_topk=16, linking_top_k=8,
                                 retrieval_top_k=6, max_retry_attempts=0, save_openie=True,
                                 openie_ner_max_tokens=MAX_TOKENS, openie_triple_max_tokens=MAX_TOKENS),
        extraction_llm=llm, qa_llm=llm, embedding_model=embedding,
        index_identity=f"public-pilot-v1:{MODEL}:thinking-disabled:max-output-2048:{EMBEDDING_REPO}@{EMBEDDING_REVISION}:e5-prefix-mean-l2-512",
    )


def create_adapter(*, weights_dir, trace_path, budget_path):
    from app.hipporag import HippoRAGAdapter
    from hipporag import Chunk

    return HippoRAGAdapter(engine_factory=lambda *, save_dir: make_engine(
        save_dir=save_dir, weights_dir=weights_dir, trace_path=trace_path, budget_path=budget_path), chunk_factory=Chunk)
