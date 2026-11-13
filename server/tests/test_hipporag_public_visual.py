"""Offline diagnostic contracts: fake HTTP/CLIP only, never a real model call."""
import copy
import importlib.util
import json
import math
from pathlib import Path

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def visual(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("public_visual_test", ROOT / "scripts/hipporag_public_visual.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    images = json.loads((ROOT / "fixtures/hipporag-public-pilot-v1/images.json").read_text())
    for image in images:
        path = tmp_path / image["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / image["path"]).read_bytes())
    monkeypatch.setattr(module, "ROOT", tmp_path)
    return module, images


def body(candidate=None, usage=True):
    if candidate is None:
        candidate = {"caption": "Synthetic test image caption.",
                     "entities": [{"label": "synthetic equipment", "bbox": [10, 20, 30, 40]}], "relations": []}
    result = {"model": "test-reported-model", "choices": [{"message": {
        "role": "assistant", "content": json.dumps(candidate)}, "finish_reason": "stop"}]}
    if usage:
        result["usage"] = {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}
    return result


def transport(monkeypatch, module, response=None, *, status=200, failure=None, raw=None):
    calls, settings = [], []
    raw = raw if raw is not None else json.dumps(response if response is not None else body()).encode()

    class Response:
        status_code = status

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def iter_bytes(self):
            if failure is not None:
                raise failure
            yield raw

    class Client:
        def __init__(self, **kwargs):
            settings.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def stream(self, method, endpoint, **kwargs):
            calls.append((method, endpoint, kwargs["json"]))
            return Response()

    monkeypatch.setattr(module.httpx, "Client", Client)
    return calls, settings


def test_visual_success_is_fixed_bounded_credential_free_and_not_gold(visual, tmp_path, monkeypatch):
    module, images = visual
    calls, settings = transport(monkeypatch, module)
    monkeypatch.setenv("OPENAI_API_KEY", "NEVER_SEND_CREDENTIAL")
    monkeypatch.setenv("HTTPS_PROXY", "http://not-an-approved-proxy.invalid")
    images[0]["reference_answer"] = "NEVER_SEND_GOLD"
    images[0]["thinking"] = {"type": "enabled"}
    report = module.collect_visual_evidence(images, tmp_path / "run")
    assert len(calls) == 2 and report["request_attempts"] == 2
    assert report["quality_evaluated"] is False
    for method, endpoint, payload in calls:
        assert method == "POST" and endpoint == "http://127.0.0.1:3425/v1/chat/completions"
        assert payload["model"] == "tencent-cloud/deepseek-v4-flash-202605"
        assert payload["max_tokens"] == 2048 and payload["temperature"] == 0
        assert payload["thinking"] == {"type": "disabled"}
        assert len(payload["messages"]) == 1
        assert len(payload["messages"][0]["content"]) == 2
        assert "NEVER_SEND" not in json.dumps(payload)
        assert "Field Site 1" not in payload["messages"][0]["content"][0]["text"]
    for config in settings:
        assert config["trust_env"] is False and config["follow_redirects"] is False
        assert config["timeout"].read == 60 and config["timeout"].connect == 5
        assert config["headers"] == {"content-type": "application/json"}
    for item in report["records"]:
        assert item["status"] == "measured_candidate" and item["evidence_kind"] == "model_candidate"
        assert item["usage"]["total_tokens"] == 25 and item["usage_status"] == "reported"
        assert item["raw_candidate"]["caption"] == item["caption"] == "Synthetic test image caption."
        assert item["returned_model"] == "test-reported-model"
        assert item["latency_ms"] >= 0
        saved = tmp_path / "run/visual" / (item["image_id"] + ".json")
        assert json.loads(saved.read_text()) == item
        assert saved.stat().st_mode & 0o777 == 0o600
    with pytest.raises(module.VisualEvidenceError, match="already_attempted"):
        module.collect_visual_evidence(images, tmp_path / "run")
    assert len(calls) == 2


@pytest.mark.parametrize("field,value", [("id", "unknown"), ("id", []), ("path", "/etc/passwd"),
                                         ("sha256", "0" * 64), ("width", 100), ("height", 100)])
def test_unknown_or_changed_input_is_rejected_before_network(visual, tmp_path, monkeypatch, field, value):
    module, images = visual
    calls, _ = transport(monkeypatch, module)
    images[1][field] = value
    with pytest.raises(module.VisualEvidenceError):
        module.collect_visual_evidence(images, tmp_path / "run")
    assert calls == [] and not (tmp_path / "run").exists()


@pytest.mark.parametrize("change", ["content", "missing", "symlink", "oversize", "duplicate", "subset"])
def test_public_files_are_hash_size_and_path_bound(visual, tmp_path, monkeypatch, change):
    module, images = visual
    calls, _ = transport(monkeypatch, module)
    path = tmp_path / images[1]["path"]
    if change == "content":
        path.write_bytes(b"not the frozen public image")
    elif change == "missing":
        path.unlink()
    elif change == "symlink":
        path.unlink()
        path.symlink_to(tmp_path / images[0]["path"])
    elif change == "oversize":
        monkeypatch.setattr(module, "MAX_IMAGE_BYTES", 1)
    elif change == "duplicate":
        images[1] = copy.deepcopy(images[0])
    else:
        images.pop()
    with pytest.raises(module.VisualEvidenceError):
        module.collect_visual_evidence(images, tmp_path / "run")
    assert not calls


@pytest.mark.parametrize("failure,expected", [(httpx.ReadTimeout("never-log-secret"), "timeout"),
                                             (httpx.ConnectError("never-log-secret"), "transport_error")])
def test_failure_does_not_retry_or_invent_candidates(visual, tmp_path, monkeypatch, failure, expected):
    module, images = visual
    calls, _ = transport(monkeypatch, module, failure=failure)
    report = module.collect_visual_evidence(images, tmp_path / "run")
    assert len(calls) == 2
    for item in report["records"]:
        assert item["status"] == expected and item["usage"] is None
        assert item["caption"] is None and not item["entities"] and not item["relations"]
    assert "never-log-secret" not in json.dumps(report)


@pytest.mark.parametrize("status,expected", [(301, "redirect_rejected"), (401, "provider_http_error"),
                                          (429, "provider_http_error"), (500, "provider_http_error")])
def test_http_failure_is_visible_without_following_or_retry(visual, tmp_path, monkeypatch, status, expected):
    module, images = visual
    calls, _ = transport(monkeypatch, module, status=status)
    report = module.collect_visual_evidence(images, tmp_path / "run")
    assert len(calls) == 2
    assert all(item["status"] == expected and item["caption"] is None for item in report["records"])


@pytest.mark.parametrize("case,expected", [("empty", "invalid_response"), ("multi", "invalid_response"),
    ("role", "invalid_response"), ("tool", "invalid_response"), ("refusal", "invalid_response"),
    ("reason", "incomplete_response"), ("filtered", "incomplete_response"), ("length", "truncated_candidate"),
    ("duplicate", "invalid_response"), ("nan", "invalid_response"), ("overflow", "invalid_response"),
    ("surrogate", "invalid_response"), ("oversize", "response_too_large")])
def test_response_protocol_is_strict(visual, tmp_path, monkeypatch, case, expected):
    module, images = visual
    value = body()
    raw = None
    if case == "empty": value["choices"] = []
    elif case == "multi": value["choices"] *= 2
    elif case == "role": value["choices"][0]["message"]["role"] = "tool"
    elif case == "tool": value["choices"][0]["message"]["tool_calls"] = [{"name": "no-tool"}]
    elif case == "refusal": value["choices"][0]["message"]["refusal"] = "refused"
    elif case == "reason": value["choices"][0]["finish_reason"] = {}
    elif case == "filtered": value["choices"][0]["finish_reason"] = "content_filter"
    elif case == "length": value["choices"][0]["finish_reason"] = "length"
    elif case == "duplicate": raw = b'{"choices":[],"choices":[]}'
    elif case == "nan": raw = b'{"number":NaN}'
    elif case == "overflow": raw = b'{"number":1e999}'
    elif case == "surrogate": raw = b'{"content":"\\ud800"}'
    else: raw = b"x" * (module.MAX_RESPONSE_BYTES + 1)
    calls, _ = transport(monkeypatch, module, value, raw=raw)
    report = module.collect_visual_evidence(images, tmp_path / "run")
    assert len(calls) == 2
    assert all(item["status"] == expected for item in report["records"])


@pytest.mark.parametrize("content", [None, "", "  \n"])
def test_empty_length_response_is_truncated_and_retains_reported_usage(visual, tmp_path, monkeypatch, content):
    module, images = visual
    value = body()
    value["choices"][0]["message"]["content"] = content
    value["choices"][0]["message"]["reasoning_content"] = "DO_NOT_KEEP_PROVIDER_REASONING"
    value["choices"][0]["finish_reason"] = "length"
    value["usage"] = {"prompt_tokens": 320, "completion_tokens": 2048, "total_tokens": 2368}
    calls, _ = transport(monkeypatch, module, value)
    report = module.collect_visual_evidence(images, tmp_path / "run")
    assert len(calls) == 2
    for item in report["records"]:
        assert item["status"] == "truncated_candidate" and item["finish_reason"] == "length"
        assert item["usage"] == value["usage"] and item["usage_status"] == "reported"
        assert item["caption"] is None and item["raw_candidate"] is None
        assert item["entities"] == [] and item["relations"] == []
        assert item["http_status"] == 200 and item["response_sha256"]
        assert json.loads((tmp_path / "run/visual" / (item["image_id"] + ".json")).read_text()) == item
    assert "DO_NOT_KEEP_PROVIDER_REASONING" not in json.dumps(report)


@pytest.mark.parametrize("usage,expected", [(None, "missing"), ({"total_tokens": 7}, "partial"),
    ({"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 99}, "inconsistent"),
    ({"total_tokens": True}, "invalid"), ({"total_tokens": -1}, "invalid")])
def test_usage_is_never_estimated_or_silently_completed(visual, tmp_path, monkeypatch, usage, expected):
    module, images = visual
    value = body(usage=False)
    if usage is not None:
        value["usage"] = usage
    transport(monkeypatch, module, value)
    report = module.collect_visual_evidence(images, tmp_path / "run")
    for item in report["records"]:
        assert item["usage_status"] == expected
        if expected in {"missing", "invalid"}:
            assert item["usage"] is None
        elif expected == "partial":
            assert item["usage"] == {"prompt_tokens": None, "completion_tokens": None, "total_tokens": 7}
        else:
            assert item["usage"] == usage


def test_invalid_boxes_and_unbound_relations_remain_only_raw_candidates(visual, tmp_path, monkeypatch):
    module, images = visual
    candidate = {"caption": "Actual test output, not a replacement caption.", "entities": [
        {"label": "outside", "bbox": [1010, 0, 20, 20]}, {"label": "valid", "bbox": [1, 2, 3, 4]}],
        "relations": [{"subject": "valid", "predicate": "connected", "object": "outside"}]}
    transport(monkeypatch, module, body(candidate))
    record = module.collect_visual_evidence(images, tmp_path / "run")["records"][0]
    assert record["status"] == "partial_candidate" and record["invalid_candidate_items"] == 2
    assert record["raw_candidate"] == candidate and record["relations"] == []
    assert record["entities"] == [candidate["entities"][1]]


@pytest.mark.parametrize("candidate,expected", [
    ({"caption": "", "entities": [], "relations": []}, "empty_candidate"),
    ({"entities": [], "relations": []}, "invalid_candidate"),
    ({"caption": 17, "entities": [], "relations": []}, "invalid_candidate"),
    ({"caption": "Only actual output", "entities": [], "relations": "not-an-array"}, "invalid_candidate"),
])
def test_empty_or_invalid_candidate_does_not_gain_a_fallback_caption(visual, tmp_path, monkeypatch, candidate, expected):
    module, images = visual
    transport(monkeypatch, module, body(candidate))
    record = module.collect_visual_evidence(images, tmp_path / "run")["records"][0]
    assert record["status"] == expected and record["raw_candidate"] == candidate
    assert record["caption"] == ("" if expected == "empty_candidate" else None)


class FakeClip:
    def __init__(self, mode="normal"):
        self.mode, self.texts, self.images = mode, [], []

    def result(self, values):
        vector = values + [0.0] * (512 - len(values))
        return {"model": "openai/clip-vit-base-patch32", "source": "synthetic-config-digest",
                "dimensions": 512, "vector": vector}

    def embed_image(self, path):
        self.images.append(path)
        if self.mode == "missing": return None
        result = self.result([1, 0] if "fig2" in path.name else [0, 1])
        if self.mode == "mismatch" and "page15" in path.name: result["source"] = "different"
        if self.mode == "nan": result["vector"][0] = float("nan")
        if self.mode == "wrong-model": result["model"] = "not-the-approved-clip"
        if self.mode == "dimensions": result["vector"].pop()
        return result

    def embed_text(self, text):
        self.texts.append(text)
        return None if self.mode == "text-fail" else self.result([0.8, 0.2])


def test_clip_uses_only_original_question_and_actual_vectors(visual, monkeypatch):
    module, images = visual
    runtime = FakeClip()
    monkeypatch.setattr(module, "_vision_runtime", lambda: runtime)
    questions = [{"id": "q01", "question": "Original test question?", "reference_answer": "NEVER_SEND_GOLD",
                  "required_evidence": [{"image_id": "NEVER_SEND_GOLD"}]}]
    result = module.image_rankings(images, questions)
    assert runtime.texts == ["Original test question?"] and len(runtime.images) == 2
    assert result["status"] == "measured" and result["provider_calls"] == 0
    assert result["quality_evaluated"] is False
    assert result["rankings"][0]["results"][0]["image_id"] == "nist-sp80082-scada-fig2"
    assert result["rankings"][0]["results"][0]["score"] == pytest.approx(0.8 / math.sqrt(0.68))
    assert result["rankings"][0]["results"][1]["score"] == pytest.approx(0.2 / math.sqrt(0.68))
    assert "NEVER_SEND_GOLD" not in json.dumps(result)
    assert any("not object localization" in value for value in result["limitations"])


@pytest.mark.parametrize("mode", ["missing", "mismatch", "nan", "text-fail", "wrong-model", "dimensions"])
def test_clip_failure_never_fabricates_rankings(visual, monkeypatch, mode):
    module, images = visual
    monkeypatch.setattr(module, "_vision_runtime", lambda: FakeClip(mode))
    result = module.image_rankings(images, [{"id": "q01", "question": "Question?"}])
    assert result["status"] == "embedding_unavailable" and result["rankings"] == []


def test_invalid_question_is_rejected_before_local_model_load(visual, monkeypatch):
    module, images = visual
    monkeypatch.setattr(module, "_vision_runtime", lambda: pytest.fail("model must not load"))
    with pytest.raises(module.VisualEvidenceError, match="question_input"):
        module.image_rankings(images, [{"id": "q01", "question": "x" * 2001}])
