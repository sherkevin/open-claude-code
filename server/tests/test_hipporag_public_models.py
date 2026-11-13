"""Offline model contract tests: no gateway call, download, or real inference."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest


SPEC = importlib.util.spec_from_file_location("hipporag_public_models_tested", Path(__file__).parents[2] / "scripts/hipporag_public_models.py")
models = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(models)
MESSAGES = [{"role": "system", "content": "Extract only evidence."}, {"role": "user", "content": "PUBLIC_PILOT_PASSAGE"}]


@pytest.fixture(autouse=True)
def block_real_http(monkeypatch):
    def denied(*_args, **_kwargs):
        pytest.fail("An offline unit test attempted a real HTTP call")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", denied)


@pytest.fixture
def llm(tmp_path):
    return models.LoopbackLLM(trace_path=tmp_path / "trace.jsonl", budget_path=tmp_path / "budget.json")


def response(**updates):
    value = {"model": "deepseek-v4.1-flash-1017", "choices": [{"message": {"role": "assistant", "content": '{"named_entities":["PLC"]}'},
             "finish_reason": "stop"}], "usage": {"prompt_tokens": 31, "completion_tokens": 12, "total_tokens": 43}}
    value.update(updates)
    return value


def fake_http(monkeypatch, value=None, *, status=200, raw=None, error=None):
    calls = []

    class Reply:
        def __init__(self):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, maximum):
            return (raw if raw is not None else json.dumps(value or response()).encode())[:maximum]

    def build(*handlers):
        def open_(request, *, timeout):
            calls.append((request, timeout, handlers))
            if error is not None:
                raise error
            return Reply()
        return SimpleNamespace(open=open_)

    monkeypatch.setattr(models.urllib.request, "build_opener", build)
    return calls


def trace(llm):
    return [json.loads(line) for line in llm.ledger.trace_path.read_text().splitlines()]


def test_fixed_authorized_model_real_usage_and_no_prompt_in_trace(llm, monkeypatch):
    calls = fake_http(monkeypatch)
    content, metadata, cache_hit = llm.infer(MESSAGES)
    assert content == '{"named_entities":["PLC"]}' and cache_hit is False
    request, timeout, handlers = calls[0]
    payload = json.loads(request.data)
    assert request.full_url == "http://127.0.0.1:3425/v1/chat/completions"
    assert payload == {"messages": MESSAGES, "model": models.MODEL, "max_tokens": 2048, "temperature": 0, "stream": False,
                       "thinking": {"type": "disabled"}}
    assert timeout == 60 and len(calls) == 1
    assert handlers[0].proxies == {} and isinstance(handlers[1], models._NoRedirect)
    assert request.get_header("Authorization") is None
    assert metadata["usage"] == {"prompt_tokens": 31, "completion_tokens": 12, "total_tokens": 43}
    assert metadata["usage_status"] == "reported"
    records = trace(llm)
    assert [item["event"] for item in records] == ["llm_attempt", "llm_result"]
    assert records[0]["call_id"] == records[1]["call_id"]
    assert records[1]["status"] == "ok" and records[1]["usage"] == metadata["usage"]
    assert all(item["thinking_mode"] == "disabled" for item in records)
    assert metadata["thinking_mode"] == "disabled"
    assert records[0]["prompt_sha256"] == hashlib.sha256(models._json(MESSAGES)).hexdigest()
    assert "PUBLIC_PILOT_PASSAGE" not in llm.ledger.trace_path.read_text()
    assert "named_entities" not in llm.ledger.trace_path.read_text()
    assert llm.ledger.trace_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("usage,status,expected", [
    (None, "missing", None), ({}, "missing", None),
    ({"prompt_tokens": 23}, "partial", {"prompt_tokens": 23}),
    ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "reported",
     {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}),
])
def test_usage_absence_never_becomes_zero(llm, monkeypatch, usage, status, expected):
    fake_http(monkeypatch, response(usage=usage))
    _, metadata, _ = llm.infer(MESSAGES)
    assert metadata["usage_status"] == status and metadata["usage"] == expected
    assert trace(llm)[1]["usage"] == expected
    if expected is None:
        assert not set(models._USAGE_KEYS).intersection(metadata)


@pytest.mark.parametrize("kwargs,expected", [({"max_new_tokens": 512}, 512), ({"max_tokens": 100}, 100),
    ({"max_completion_tokens": 128, "response_format": {"type": "json_object"}}, 128)])
def test_upstream_token_aliases_are_bounded(llm, monkeypatch, kwargs, expected):
    calls = fake_http(monkeypatch)
    llm.infer(MESSAGES, **kwargs)
    payload = json.loads(calls[0][0].data)
    assert payload["max_tokens"] == expected
    assert "max_new_tokens" not in payload and "max_completion_tokens" not in payload


@pytest.mark.parametrize("kwargs", [{"model": "OTHER"}, {"endpoint": "https://external.invalid"}, {"max_tokens": 2049},
    {"max_tokens": True}, {"max_tokens": 0}, {"max_tokens": 1, "max_new_tokens": 2}, {"temperature": 1},
    {"response_format": {"type": "json_schema", "json_schema": {}}}, {"tools": []},
    {"thinking": {"type": "enabled"}}, {"thinking": {"type": "disabled"}},
    {"extra_body": {"thinking": {"type": "enabled"}}}, {"reasoning_effort": "high"}])
def test_unapproved_parameters_fail_before_budget_or_network(llm, monkeypatch, kwargs):
    calls = fake_http(monkeypatch)
    with pytest.raises(models.PublicModelError, match="invalid_input"):
        llm.infer(MESSAGES, **kwargs)
    assert calls == [] and not llm.ledger.budget_path.exists()


@pytest.mark.parametrize("messages", [[], [{"role": "tool", "content": "untrusted"}],
    [{"role": "user", "content": [{"type": "text", "text": "not a text-only call"}]}],
    [{"role": "user", "content": "x", "name": "unapproved"}], [{"role": "user", "content": "\ud800"}],
    [{"role": "user", "content": "x" * 512001}]])
def test_invalid_message_input_does_not_dispatch(llm, messages):
    with pytest.raises(models.PublicModelError, match="invalid_input"):
        llm.infer(messages)
    assert not llm.ledger.budget_path.exists()


@pytest.mark.parametrize("error,code", [(TimeoutError("SECRET"), "model_timeout"),
    (urllib.error.URLError(TimeoutError("SECRET")), "model_timeout"),
    (urllib.error.HTTPError("PRIVATE", 429, "SECRET", {}, None), "model_http_error"),
    (OSError("SECRET"), "model_transport_error")])
def test_single_failed_attempt_consumes_budget_and_sanitizes(llm, monkeypatch, error, code):
    calls = fake_http(monkeypatch, error=error)
    with pytest.raises(models.PublicModelError) as caught:
        llm.infer(MESSAGES)
    assert caught.value.code == code and "SECRET" not in str(caught.value)
    assert len(calls) == 1 and json.loads(llm.ledger.budget_path.read_text())["used"] == 1
    assert trace(llm)[-1]["status"] == "error" and "SECRET" not in llm.ledger.trace_path.read_text()


@pytest.mark.parametrize("kind", ["invalid_json", "duplicate_key", "nan", "oversize", "not_object", "choices_empty",
    "usage_bool", "usage_negative", "usage_inconsistent", "tool_calls", "refusal", "finish_list", "returned_model_payload"])
def test_invalid_response_is_not_used_as_extraction(llm, monkeypatch, kind):
    value, raw = response(), None
    if kind == "invalid_json": raw = b"not json"
    elif kind == "duplicate_key": raw = b'{"choices": [], "choices": []}'
    elif kind == "nan": raw = b'{"choices": [], "unrelated": NaN}'
    elif kind == "oversize": raw = b"x" * (models.MAX_RESPONSE_BYTES + 1)
    elif kind == "not_object": raw = b"[]"
    elif kind == "choices_empty": value["choices"] = []
    elif kind == "usage_bool": value["usage"]["prompt_tokens"] = True
    elif kind == "usage_negative": value["usage"]["total_tokens"] = -1
    elif kind == "usage_inconsistent": value["usage"]["total_tokens"] = 999
    elif kind == "tool_calls": value["choices"][0]["message"]["tool_calls"] = [{"id": "no"}]
    elif kind == "refusal": value["choices"][0]["message"]["refusal"] = "no"
    elif kind == "finish_list": value["choices"][0]["finish_reason"] = ["PRIVATE_RAW_VALUE"]
    else: value["model"] = "PRIVATE RAW VALUE"
    calls = fake_http(monkeypatch, value, raw=raw)
    with pytest.raises(models.PublicModelError, match="model_protocol_error"):
        llm.infer(MESSAGES)
    assert len(calls) == 1 and trace(llm)[-1]["status"] == "error"
    assert "PRIVATE" not in llm.ledger.trace_path.read_text()
    if kind == "usage_inconsistent":
        assert trace(llm)[-1]["usage"]["total_tokens"] == 999


def test_truncated_response_records_usage_but_is_not_accepted(llm, monkeypatch):
    value = response()
    value["choices"][0]["finish_reason"] = "length"
    fake_http(monkeypatch, value)
    with pytest.raises(models.PublicModelError, match="model_truncated"):
        llm.infer(MESSAGES)
    assert trace(llm)[-1]["usage"]["total_tokens"] == 43


@pytest.mark.parametrize("failure", ["truncated", "timeout", "http", "protocol", "invalid_input", "budget"])
def test_first_failure_opens_instance_circuit_without_more_http_or_budget(llm, monkeypatch, failure):
    value, error, kwargs = response(), None, {}
    if failure == "truncated": value["choices"][0]["finish_reason"] = "length"
    elif failure == "timeout": error = TimeoutError("PRIVATE")
    elif failure == "http": error = urllib.error.HTTPError("PRIVATE", 429, "PRIVATE", {}, None)
    elif failure == "protocol": value["choices"] = []
    elif failure == "invalid_input": kwargs = {"thinking": {"type": "enabled"}}
    else:
        for _ in range(64): llm.ledger.reserve()
    calls = fake_http(monkeypatch, value, error=error)
    with pytest.raises(models.PublicModelError) as first:
        llm.infer(MESSAGES, **kwargs)
    http_count = len(calls)
    reserved = llm.ledger.budget_path.read_bytes() if llm.ledger.budget_path.exists() else None
    for _ in range(3):
        with pytest.raises(models.PublicModelError, match="model_circuit_open"):
            llm.infer(MESSAGES)
    assert len(calls) == http_count
    assert (llm.ledger.budget_path.read_bytes() if llm.ledger.budget_path.exists() else None) == reserved
    denied = trace(llm)[-3:]
    assert all(item["event"] == "llm_denied" and item["error_code"] == "model_circuit_open"
               and item["first_error_code"] == first.value.code and item["thinking_mode"] == "disabled"
               and item["usage"] is None and "sequence" not in item for item in denied)


def test_audit_failure_also_latches_circuit_after_storage_recovers(llm, monkeypatch):
    llm.ledger.trace_path.write_text("nonprivate trace")
    llm.ledger.trace_path.chmod(0o644)
    calls = fake_http(monkeypatch)
    with pytest.raises(models.PublicModelError, match="audit_unavailable"):
        llm.infer(MESSAGES)
    llm.ledger.trace_path.write_text("")
    llm.ledger.trace_path.chmod(0o600)
    with pytest.raises(models.PublicModelError, match="model_circuit_open"):
        llm.infer(MESSAGES)
    assert calls == [] and trace(llm)[-1]["first_error_code"] == "audit_unavailable"


def test_waiting_infer_cannot_dispatch_after_first_thread_fails(llm, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []
    def build(*handlers):
        def open_(request, *, timeout):
            calls.append(request)
            started.set()
            assert release.wait(3)
            raise TimeoutError()
        return SimpleNamespace(open=open_)
    monkeypatch.setattr(models.urllib.request, "build_opener", build)
    def infer():
        try: llm.infer(MESSAGES)
        except models.PublicModelError as exc: return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(infer)
        assert started.wait(3)
        second = pool.submit(infer)
        release.set()
        assert first.result() == "model_timeout" and second.result() == "model_circuit_open"
    assert len(calls) == 1 and json.loads(llm.ledger.budget_path.read_text())["used"] == 1


def test_success_does_not_open_circuit(llm, monkeypatch):
    calls = fake_http(monkeypatch)
    llm.infer(MESSAGES)
    llm.infer(MESSAGES)
    assert len(calls) == 2 and all(item["thinking_mode"] == "disabled" for item in trace(llm))


def test_circuit_is_instance_local_and_never_retries_implicitly(llm, tmp_path, monkeypatch):
    calls = fake_http(monkeypatch, error=TimeoutError())
    with pytest.raises(models.PublicModelError): llm.infer(MESSAGES)
    assert len(calls) == 1
    succeeding_calls = fake_http(monkeypatch)
    with pytest.raises(models.PublicModelError, match="model_circuit_open"): llm.infer(MESSAGES)
    fresh = models.LoopbackLLM(trace_path=llm.ledger.trace_path, budget_path=llm.ledger.budget_path)
    fresh.infer(MESSAGES)
    assert len(succeeding_calls) == 1 and json.loads(llm.ledger.budget_path.read_text())["used"] == 2


def test_redirect_is_never_followed():
    with pytest.raises(models.PublicModelError, match="model_http_error"):
        models._NoRedirect().redirect_request(None, None, 307, "redirect", {}, "https://external.invalid")


def test_budget_is_shared_atomically_by_multiple_instances(tmp_path):
    path = tmp_path / "budget.json"
    ledgers = [models.CallLedger(trace_path=tmp_path / "trace.jsonl", budget_path=path) for _ in range(4)]
    def reserve(index):
        try:
            return ledgers[index % len(ledgers)].reserve()
        except models.PublicModelError as exc:
            assert exc.code == "budget_exhausted"
            return None
    with ThreadPoolExecutor(max_workers=12) as pool:
        values = list(pool.map(reserve, range(80)))
    assert sorted(value for value in values if value is not None) == list(range(1, 65))
    assert json.loads(path.read_text())["used"] == 64


def test_budget_exhaustion_is_audited_without_http(llm, monkeypatch):
    for _ in range(64): llm.ledger.reserve()
    calls = fake_http(monkeypatch)
    with pytest.raises(models.PublicModelError, match="budget_exhausted"):
        llm.infer(MESSAGES)
    assert calls == [] and trace(llm)[-1]["event"] == "llm_denied"


@pytest.mark.parametrize("kind", ["bad_json", "wrong_limit", "bool_count", "symlink", "hardlink", "permissions"])
def test_untrusted_budget_never_resets_or_calls_http(llm, tmp_path, monkeypatch, kind):
    value = {"schema": "hipporag-public-budget/v1", "limit": 64, "used": 1}
    path = llm.ledger.budget_path
    if kind == "wrong_limit": value["limit"] = 1000
    if kind == "bool_count": value["used"] = True
    path.write_text("broken" if kind == "bad_json" else json.dumps(value))
    path.chmod(0o600)
    if kind == "symlink":
        other = tmp_path / "other"
        path.rename(other)
        path.symlink_to(other)
    elif kind == "hardlink": os.link(path, tmp_path / "other")
    elif kind == "permissions": path.chmod(0o644)
    calls = fake_http(monkeypatch)
    with pytest.raises(models.PublicModelError, match="audit_unavailable"):
        llm.infer(MESSAGES)
    assert calls == []


def test_trace_failure_prevents_http_and_consumes_reservation(llm, monkeypatch):
    llm.ledger.trace_path.write_text("existing nonprivate log")
    llm.ledger.trace_path.chmod(0o644)
    calls = fake_http(monkeypatch)
    with pytest.raises(models.PublicModelError, match="audit_unavailable"):
        llm.infer(MESSAGES)
    assert calls == [] and json.loads(llm.ledger.budget_path.read_text())["used"] == 1


@pytest.fixture
def weights(tmp_path):
    directory = tmp_path / "weights"
    directory.mkdir()
    files = {}
    for name in models.EMBEDDING_FILES:
        contents = ("synthetic-download-contract-file:" + name).encode()
        (directory / name).write_bytes(contents)
        files[name] = hashlib.sha256(contents).hexdigest()
    (directory / "revision.json").write_text(json.dumps({"repo": models.EMBEDDING_REPO,
        "revision": models.EMBEDDING_REVISION, "files": files}))
    return directory


def test_exact_weight_receipt_is_required(weights):
    assert models.verify_weights(weights) == weights


@pytest.mark.parametrize("change", ["contents", "revision", "repo", "missing", "extra", "symlink", "hardlink", "receipt_fifo"])
def test_untrusted_weights_fail_before_loading_transformers(weights, change):
    if change == "contents": (weights / "model.safetensors").write_text("tampered")
    elif change in {"revision", "repo"}:
        receipt = json.loads((weights / "revision.json").read_text())
        receipt[change] = "unapproved"
        (weights / "revision.json").write_text(json.dumps(receipt))
    elif change == "missing": (weights / "README.md").unlink()
    elif change == "extra": (weights / "pytorch_model.bin").touch()
    elif change == "receipt_fifo":
        (weights / "revision.json").unlink()
        os.mkfifo(weights / "revision.json")
    else:
        target = weights / "README.md"
        target.unlink()
        if change == "symlink": target.symlink_to(weights / "config.json")
        else: os.link(weights / "config.json", target)
    with pytest.raises(models.PublicModelError, match="weights_untrusted"):
        models.LocalE5Embedding(weights)


@pytest.mark.parametrize("source_kind", ["python", "numpy_unique", "numpy_scalar", "numpy_tuple"])
def test_local_e5_uses_pinned_safe_cpu_loading_prefix_masked_pooling(weights, monkeypatch, source_kind):
    torch = pytest.importorskip("torch")
    np = pytest.importorskip("numpy")
    calls, token_batches = [], []

    class Tokenizer:
        def __call__(self, texts, **kwargs):
            token_batches.append(texts)
            assert kwargs == {"max_length": 512, "padding": True, "truncation": True, "return_tensors": "pt"}
            return {"attention_mask": torch.tensor([[1, 1, 0]] * len(texts))}

    class Model:
        config = SimpleNamespace(hidden_size=384)
        def to(self, device):
            assert device == "cpu"
            return self
        def eval(self): return self
        def __call__(self, attention_mask):
            hidden = torch.zeros((len(attention_mask), 3, 384))
            hidden[:, 0, 0] = 3
            hidden[:, 1, 1] = 4
            hidden[:, 2, 2] = 1000
            return SimpleNamespace(last_hidden_state=hidden)

    def load(kind, value):
        def loader(path, **kwargs):
            calls.append((kind, path, kwargs))
            return value
        return SimpleNamespace(from_pretrained=loader)
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(AutoTokenizer=load("tokenizer", Tokenizer()), AutoModel=load("model", Model())))
    embedding = models.LocalE5Embedding(weights)
    source = {
        "python": ["source passage"],
        "numpy_unique": list(np.unique(["source passage", "source passage"])),
        "numpy_scalar": np.str_("source passage"),
        "numpy_tuple": (np.str_("source passage"),),
    }[source_kind]
    values = embedding.batch_encode(source)
    embedding.batch_encode("current query", instruction="upstream query instruction", norm=True)
    embedding.batch_encode(["explicit query"], query=True)
    assert token_batches == [["passage: source passage"], ["query: current query"], ["query: explicit query"]]
    assert np.allclose(values[0, :3], [.6, .8, 0]) and values.shape == (1, 384)
    assert values.dtype == np.float32 and embedding.batch_encode([]).shape == (0, 384)
    assert embedding.query_instruction_mode == "shared"
    assert calls == [("tokenizer", str(weights), {"local_files_only": True, "trust_remote_code": False}),
                     ("model", str(weights), {"local_files_only": True, "trust_remote_code": False, "use_safetensors": True})]


@pytest.mark.parametrize("kind", ["none", "integer", "bool", "bytes", "mixed", "numpy_bytes", "array", "subclass", "object", "numpy_empty"])
def test_local_e5_rejects_nontext_without_implicit_string_conversion(kind):
    np = pytest.importorskip("numpy")

    class UnknownString(str):
        def __str__(self):
            pytest.fail("Unknown subclasses must not be stringified")

    class UnknownObject:
        def __str__(self):
            pytest.fail("Arbitrary objects must not be stringified")

    source = {"none": None, "integer": 1, "bool": True, "bytes": b"text", "mixed": ["valid", 1],
              "numpy_bytes": [np.bytes_("text")], "array": np.array(["text"]),
              "subclass": [UnknownString("text")], "object": [UnknownObject()],
              "numpy_empty": [np.str_(" ")]}[kind]
    embedding = models.LocalE5Embedding.__new__(models.LocalE5Embedding)
    with pytest.raises(models.PublicModelError, match="invalid_input"):
        embedding.batch_encode(source)


def test_make_engine_uses_real_injected_components_and_no_provider_defaults(tmp_path, monkeypatch):
    captured = {}
    sentinel = object()
    def engine(**kwargs):
        captured.update(kwargs)
        return sentinel
    monkeypatch.setitem(sys.modules, "hipporag", SimpleNamespace(HippoRAG=engine))
    monkeypatch.setitem(sys.modules, "hipporag.utils.config_utils", SimpleNamespace(BaseConfig=lambda **kwargs: SimpleNamespace(**kwargs)))
    monkeypatch.setattr(models, "LocalE5Embedding", lambda directory: ("local-e5", directory))
    result = models.make_engine(save_dir=tmp_path / "index", weights_dir=tmp_path / "weights", trace_path=tmp_path / "trace", budget_path=tmp_path / "budget")
    assert result is sentinel and isinstance(captured["extraction_llm"], models.LoopbackLLM)
    assert captured["extraction_llm"] is captured["qa_llm"]
    assert captured["global_config"].max_retry_attempts == 0
    assert captured["global_config"].openie_ner_max_tokens == captured["global_config"].openie_triple_max_tokens == 2048
    assert captured["global_config"].llm_name == models.MODEL
    assert models.EMBEDDING_REVISION in captured["index_identity"]
    assert "thinking-disabled:max-output-2048" in captured["index_identity"]
    assert not (tmp_path / "budget").exists()


def test_factory_manifest_metadata_matches_injected_endpoint_and_tokenizer_limit(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "hipporag", SimpleNamespace(HippoRAG=lambda **kwargs: kwargs["global_config"]))
    monkeypatch.setitem(sys.modules, "hipporag.utils.config_utils", SimpleNamespace(BaseConfig=lambda **kwargs: SimpleNamespace(**kwargs)))
    monkeypatch.setattr(models, "LocalE5Embedding", lambda directory: None)
    config = models.make_engine(save_dir=tmp_path / "index", weights_dir=tmp_path / "weights",
                                trace_path=tmp_path / "trace", budget_path=tmp_path / "budget")
    assert config.llm_base_url == "http://127.0.0.1:3425/v1"
    assert config.llm_base_url + "/chat/completions" == models.ENDPOINT
    assert config.embedding_max_seq_len == 512
