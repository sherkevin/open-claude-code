import asyncio
import sys
from pathlib import Path

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import concurrency_load  # noqa: E402
import trial_smoke  # noqa: E402


def test_percentile_uses_linear_interpolation():
    assert concurrency_load.percentile([], 0.5) is None
    assert concurrency_load.percentile([10], 0.99) == 10
    assert concurrency_load.percentile([10, 20, 30, 40], 0.50) == 25.0
    assert concurrency_load.percentile([10, 20, 30, 40], 0.95) == 38.5
    assert concurrency_load.percentile([40, 10, 30, 20], 0.99) == pytest.approx(39.7)
    summary = concurrency_load.latency_summary([10, 20, 30, 40])
    assert summary == {"count": 4, "p50": 25.0, "p95": 38.5, "p99": pytest.approx(39.7), "max": 40}


def test_load_config_rejects_unknown_scenarios_and_empty_levels():
    with pytest.raises(ValueError):
        concurrency_load.LoadConfig(scenarios=("chat", "unknown"))
    with pytest.raises(ValueError):
        concurrency_load.LoadConfig(levels=())
    with pytest.raises(ValueError):
        concurrency_load.LoadConfig(iterations=0)


class _FakeStreamResponse:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamClient:
    """First connection ends after a stale snapshot; the replay continues."""

    def __init__(self):
        self.headers = []

    def stream(self, method, url, headers=None, timeout=None):
        self.headers.append(dict(headers or {}))
        if len(self.headers) == 1:
            return _FakeStreamResponse(
                [
                    "id: 1",
                    "event: run.started",
                    "data: {}",
                    "",
                    "id: 2",
                    "event: message.delta",
                    "data: {}",
                    "",
                ]
            )
        return _FakeStreamResponse(["id: 3", "event: run.completed", "data: {\"status\": \"succeeded\"}", ""])


def test_sse_helper_resumes_from_last_event_id_after_replay_boundary():
    fake = _FakeStreamClient()
    stream = asyncio.run(trial_smoke.stream_run_events(fake, "run-replay", timeout=5.0))
    assert [item["sse_type"] for item in stream["events"]] == ["run.started", "message.delta", "run.completed"]
    assert [item["sse_id"] for item in stream["events"]] == ["1", "2", "3"]
    assert stream["connections"] == 2
    assert "Last-Event-ID" not in fake.headers[0]
    assert fake.headers[1]["Last-Event-ID"] == "2"


class _PreResponseFailure:
    async def __aenter__(self):
        raise httpx.ReadError("")

    async def __aexit__(self, *exc_info):
        return False


class _ResponseStartedFailure:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def aiter_lines(self):
        # Headers and a keep-alive comment arrived, but no complete SSE event.
        yield ": keepalive"
        raise httpx.ReadError("")


class _FailureThenSuccessClient:
    def __init__(self, failure):
        self.failure = failure
        self.calls = 0
        self.headers = []

    def stream(self, method, url, headers=None, timeout=None):
        self.calls += 1
        self.headers.append(dict(headers or {}))
        if self.calls == 1:
            return self.failure
        return _FakeStreamResponse(["id: 3", "event: run.completed", "data: {\"status\": \"succeeded\"}", ""])


def test_sse_helper_only_classifies_provable_pre_response_failures_as_stale():
    fake = _FailureThenSuccessClient(_PreResponseFailure())
    stream = asyncio.run(trial_smoke.stream_run_events(fake, "run-stale", timeout=5.0))
    assert stream["stale_connection_retries"] == 1
    assert stream["unknown_transport_retries"] == 0
    assert stream["transport_failures"] == 1
    attempt = stream["transport_attempts"][0]
    assert attempt["classification"] == "stale_connection_pre_response"
    assert attempt["response_started"] is False
    assert attempt["body_bytes_at_failure"] == 0


def test_sse_helper_counts_post_response_failures_as_unknown_reconnects():
    fake = _FailureThenSuccessClient(_ResponseStartedFailure())
    stream = asyncio.run(trial_smoke.stream_run_events(fake, "run-unknown", timeout=5.0))
    assert stream["stale_connection_retries"] == 0
    assert stream["unknown_transport_retries"] == 1
    assert stream["transport_failures"] == 1
    assert stream["connections"] == 2
    attempt = stream["transport_attempts"][0]
    assert attempt["classification"] == "unknown_transport_retry"
    assert attempt["response_started"] is True
    assert attempt["body_bytes_at_failure"] > 0
    # Unknown failures are not subtracted from the reconnect count.
    reconnects = stream["connections"] - 1 - stream["stale_connection_retries"]
    assert reconnects == 1



def test_trial_smoke_covers_vertical_slice_without_external_resources(tmp_path):
    report = asyncio.run(
        trial_smoke.run_trial_smoke(trial_smoke.TrialSmokeConfig(data_dir=tmp_path / "trial-smoke"))
    )
    assert report["synthetic"] is True
    assert report["summary"]["total"] == 21, report["checks"]
    assert report["status"] == "passed", report["checks"]
    assert report["summary"]["failed"] == 0
    names = {item["name"] for item in report["checks"]}
    assert {
        "health_live",
        "login_admin",
        "retrieval_bm25",
        "chat_with_attachment",
        "sse_stream",
        "cancel_run",
        "conversation_isolation",
        "member_isolation",
        "trial_deployment_policy",
        "trial_allowed_reads_and_chat",
        "trial_bm25_and_hybrid",
        "trial_reverse_boundaries",
    } <= names
    assert report["runtime"]["model_calls"] == 0
    assert report["runtime"]["executions"] >= 3
    assert any("真实模型" in item for item in report["external_validation_limits"])


def test_concurrency_matrix_records_every_layer_and_metric(tmp_path):
    config = concurrency_load.LoadConfig(
        levels=(1, 5),
        iterations=1,
        timeout=10.0,
        data_dir=tmp_path / "concurrency-load",
    )
    report = asyncio.run(concurrency_load.run_load(config))
    assert report["synthetic"] is True
    assert report["summary"]["status"] == "passed", report["matrix"]
    assert report["summary"]["errors"] == 0
    assert report["summary"]["timeouts"] == 0
    assert report["summary"]["model_calls"] == 0
    assert report["runtime"]["model_calls"] == 0
    assert report["config"]["capacity_script"] == "scripts/trial_capacity.py"
    assert report["config"]["max_active_sessions"] == 4
    assert report["config"]["execution_slots_are_deployment_input"] is True
    rows = {(item["scenario"], item["level"]): item for item in report["matrix"]}
    expected = {(scenario, level) for scenario in config.scenarios for level in config.levels}
    assert set(rows) == expected
    for (scenario, level), row in rows.items():
        assert row["requests"] == level * config.iterations
        assert row["ok"] == row["requests"]
        assert row["error_count"] == 0
        assert row["latency_ms"]["p50"] is not None
        assert row["latency_ms"]["p95"] >= row["latency_ms"]["p50"]
        assert row["latency_ms"]["p99"] >= row["latency_ms"]["p95"]
        assert row["throughput_rps"] > 0
        assert row["resources"]["rss_peak_bytes"] and row["resources"]["rss_peak_bytes"] > 0
        if scenario == "sse":
            assert row["first_byte_ms"]["p50"] is not None
        else:
            assert row["first_byte_ms"] is None
        if row["execution_layer"] and level <= report["config"]["max_active_sessions"]:
            assert row["completed"] == row["requests"]
        elif row["execution_layer"]:
            assert row["capacity_rejected"] > 0
            assert row["classified_executions"] == row["requests"]
        if scenario == "cancel":
            # With the default four slots, some members of a larger burst can
            # be rejected before they ever become cancelable.  Every request
            # still has to be either a real cancellation or an explicit
            # capacity rejection; it must not disappear as a silent queue.
            assert row["cancellations"] + row["capacity_rejected"] == row["requests"]
    markdown = concurrency_load.render_markdown(report)
    assert "并发矩阵" in markdown
    assert "无法在无外部资源下验收的产品功能" in markdown


def test_trial_soak_short_probe_records_sse_and_shutdown(tmp_path):
    import trial_soak

    report = asyncio.run(
        trial_soak.run_acceptance(
            profile="control",
            duration=1.0,
            virtual_users=2,
            stream_count=1,
            timeout=10.0,
            pace=0.05,
            data_dir=tmp_path / "trial-soak",
        )
    )
    assert report["synthetic"] is True
    assert report["acceptance"]["status"] == "failed"
    assert any("duration below acceptance minimum" in item for item in report["acceptance"]["issues"])
    assert report["metrics"]["operations"]["sse"]["requests"] >= 1
    assert report["metrics"]["operations"]["sse"]["ok"] >= 1
    assert report["streams"][0]["terminal_events"] == 1
    assert report["streams"][0]["terminal_count_in_trace"] == 1
    assert report["acceptance"]["shutdown_ok"] is True
