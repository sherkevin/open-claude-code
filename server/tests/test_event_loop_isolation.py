"""Regressions for SQLite writes blocking the asyncio event loop.

With ``synchronous=FULL`` every commit fsyncs.  On the school-cloud data disk a
single fsync measured roughly 80 ms, so running ``prepare_run`` or an actor's
durable event transaction directly on the event-loop thread stalled SSE,
heartbeats and every other concurrent request (evidence:
``.runtime/evidence/probe_prepare_run_loop.json`` and
``probe_message_send_block_full.json``).

These tests inject a deterministic slow transaction, then assert:

* every instrumented database call runs on a worker thread, never on the
  thread that owns the event loop;
* an independent heartbeat coroutine keeps ticking while message submission,
  plan preparation, SSE polling and durable event persistence are in flight;
* concurrent submissions all return and every run reaches a terminal state
  (no permanent queueing);
* cancellation, duplicate submission and shutdown keep their ordering and
  idempotency: exactly one ``run.completed``, strictly increasing event ids and
  the terminal event last.

The injected delay uses ``time.sleep`` (not CPU spin) so it models an fsync:
the GIL is released, and the *only* way the loop stays responsive is that the
blocking call itself was moved off it.
"""
import asyncio
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from app.db import offload_write

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import trial_smoke  # noqa: E402

PASSWORD = trial_smoke.PASSWORD


class LoopProbe:
    """Independent heartbeat measuring event-loop scheduling gaps."""

    def __init__(self, interval: float = 0.005):
        self.interval = interval
        self.gaps: list[float] = []
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "LoopProbe":
        self._task = asyncio.create_task(self._run())
        return self

    async def _run(self) -> None:
        last = time.perf_counter()
        while not self._stop.is_set():
            await asyncio.sleep(self.interval)
            current = time.perf_counter()
            self.gaps.append(current - last)
            last = current

    async def __aexit__(self, *exc_info) -> bool:
        self._stop.set()
        await self._task
        return False

    @property
    def max_gap(self) -> float:
        return max(self.gaps, default=0.0)

    def percentile(self, fraction: float) -> float:
        if not self.gaps:
            return 0.0
        ordered = sorted(self.gaps)
        index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
        return ordered[index]


class SlowDatabase:
    """Wrap Database's public methods in a synchronous, fsync-like delay.

    Every call records the executing thread.  ``loop_thread`` is captured on
    entry (the test's own event-loop thread), so any database work still made
    directly from an async endpoint shows up as an on-loop violation instead of
    merely a timing blip.
    """

    METHODS = ("transaction", "execute", "executemany", "one", "all")

    def __init__(self, db, seconds: float = 0.05):
        self.db = db
        self.seconds = seconds
        self.loop_thread = threading.get_ident()
        self.calls: list[tuple[str, int]] = []
        self.on_loop_stacks: list[tuple[str, str]] = []
        self._originals: dict[str, object] = {}

    def _wrap(self, name, original):
        def slow(*args, **kwargs):
            ident = threading.get_ident()
            if ident == self.loop_thread:
                import traceback

                self.on_loop_stacks.append((name, "".join(traceback.format_stack())))
            self.calls.append((name, ident))
            time.sleep(self.seconds)
            return original(*args, **kwargs)

        return slow

    def __enter__(self) -> "SlowDatabase":
        for name in self.METHODS:
            original = getattr(self.db, name)
            self._originals[name] = original
            setattr(self.db, name, self._wrap(name, original))
        return self

    def __exit__(self, *exc_info) -> bool:
        for name, original in self._originals.items():
            setattr(self.db, name, original)
        return False

    @property
    def on_loop_calls(self) -> list[tuple[str, int]]:
        return [call for call in self.calls if call[1] == self.loop_thread]

    def assert_all_off_loop(self) -> None:
        assert self.calls, "the instrumented database path was never exercised"
        assert not self.on_loop_calls, (
            "SQLite work still ran on the event-loop thread: "
            f"{sorted({name for name, _ in self.on_loop_calls})}\n"
            + "\n".join(stack for _, stack in self.on_loop_stacks)
        )


class ImmediateRuntime:
    """Deterministic runtime: a few ordered deltas, then usage."""

    deltas = 3

    def __init__(self, service, conversation_id, config, approve, emit):
        self.emit = emit

    async def connect(self) -> None:
        return None

    async def execute(self, run, message) -> None:
        for index in range(type(self).deltas):
            await self.emit(
                "message.delta",
                {"message_id": run["assistant_message_id"], "text": f"片段{index}|"},
            )
        await self.emit(
            "usage.updated",
            {
                "input_tokens": 12,
                "output_tokens": 4,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "estimated_cost_usd": 0,
                "currency": "USD",
                "cost_is_estimate": False,
                "source": "event-loop-isolation-test",
                "model_call_count": 0,
            },
        )

    async def interrupt(self) -> None:
        return None

    async def close(self) -> None:
        return None


class GatedTailRuntime:
    """Parked until released; then emits one final delta and returns."""

    started = asyncio.Event()
    release = asyncio.Event()

    @classmethod
    def reset(cls) -> None:
        cls.started = asyncio.Event()
        cls.release = asyncio.Event()

    def __init__(self, service, conversation_id, config, approve, emit):
        self.emit = emit

    async def connect(self) -> None:
        return None

    async def execute(self, run, message) -> None:
        type(self).started.set()
        await type(self).release.wait()
        await self.emit("message.delta", {"message_id": run["assistant_message_id"], "text": "尾部隔离验证"})

    async def interrupt(self) -> None:
        type(self).release.set()

    async def close(self) -> None:
        return None


class GatedCancelRuntime:
    """Parked until cancelled; mirrors a real runtime's CancelledError."""

    started = asyncio.Event()
    release = asyncio.Event()

    @classmethod
    def reset(cls) -> None:
        cls.started = asyncio.Event()
        cls.release = asyncio.Event()

    def __init__(self, service, conversation_id, config, approve, emit):
        self.emit = emit

    async def connect(self) -> None:
        return None

    async def execute(self, run, message) -> None:
        type(self).started.set()
        await type(self).release.wait()
        raise asyncio.CancelledError()

    async def interrupt(self) -> None:
        type(self).release.set()

    async def close(self) -> None:
        return None


async def _login(client) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": "synthetic-admin", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text


async def _assistant(client) -> dict:
    return await trial_smoke.request_json(
        client,
        "POST",
        "/api/v1/assistants",
        expected=(201,),
        json={"name": "事件循环隔离助手", "instructions": "仅用于学校云合成回归。", "knowledge_base_ids": [], "connections": [], "web_search_enabled": False},
    )


async def _conversation(client, assistant: dict, title: str) -> dict:
    return await trial_smoke.request_json(
        client,
        "POST",
        "/api/v1/conversations",
        expected=(201,),
        json={"assistant_id": assistant["id"], "title": title},
    )


def _assert_terminal_ordering(stream: dict) -> dict:
    """Exactly one terminal event, ids strictly increasing, terminal last."""
    ids = [int(item["sse_id"]) for item in stream["events"]]
    assert ids == sorted(ids) and len(ids) == len(set(ids)), ids
    types = trial_smoke.event_types(stream)
    assert types.count("run.completed") == 1, types
    assert types[-1] == "run.completed", types
    return trial_smoke.sse_payloads(stream, "run.completed")[0]["data"]


async def _scenario_prepare_run_and_emit(tmp_path: Path) -> None:
    server = await trial_smoke.SyntheticTrialServer.start(
        tmp_path / "loop-prepare",
        max_active_sessions=8,
        idle_seconds=60,
        runtime_factory=ImmediateRuntime,
    )
    server.create_admin()
    service = server.app.state.service
    try:
        async with trial_smoke.make_client(server.base_url, timeout=30.0, max_connections=16) as client:
            await _login(client)
            assistant = await _assistant(client)
            conversation = await _conversation(client, assistant, "提交隔离")
            slow = SlowDatabase(service.db, seconds=0.06)
            with slow:
                async with LoopProbe() as probe:
                    submitted = await client.post(
                        f"/api/v1/conversations/{conversation['id']}/messages",
                        json={"client_request_id": "loop-prepare-1", "text": "事件循环隔离", "file_ids": []},
                    )
                assert submitted.status_code == 202, submitted.text
                run_id = submitted.json()["run_id"]
            slow.assert_all_off_loop()
            # A committed prepare_run and every emitted delta must have gone
            # through the instrumented off-loop path.
            assert len(slow.calls) >= 5, slow.calls
            assert probe.max_gap < 0.25, f"event loop stalled for {probe.max_gap:.3f}s during submit"
            assert probe.percentile(0.95) < 0.10, f"p95 loop gap {probe.percentile(0.95):.3f}s"

            run = await trial_smoke.wait_run(client, run_id, timeout=20.0)
            assert run["status"] == "succeeded", run
            messages = await trial_smoke.request_json(client, "GET", f"/api/v1/conversations/{conversation['id']}/messages")
            assistant_text = messages[-1]["text"]
            assert assistant_text == "片段0|片段1|片段2|", assistant_text

            stream = await trial_smoke.stream_run_events(client, run_id, timeout=15.0)
            assert stream["connections"] == 1, "terminal event forced a reconnect"
            terminal = _assert_terminal_ordering(stream)
            assert terminal["status"] == "succeeded", terminal
            delta_text = "".join(
                item["event"]["data"]["text"]
                for item in stream["events"]
                if item["sse_type"] == "message.delta"
            )
            assert delta_text == assistant_text, (delta_text, assistant_text)
    finally:
        await server.stop()


async def _scenario_sse_polling(tmp_path: Path) -> None:
    GatedTailRuntime.reset()
    server = await trial_smoke.SyntheticTrialServer.start(
        tmp_path / "loop-sse",
        max_active_sessions=4,
        idle_seconds=60,
        runtime_factory=GatedTailRuntime,
    )
    server.create_admin()
    service = server.app.state.service
    try:
        async with trial_smoke.make_client(server.base_url, timeout=30.0, max_connections=16) as client:
            await _login(client)
            assistant = await _assistant(client)
            conversation = await _conversation(client, assistant, "SSE 轮询隔离")
            submitted = await client.post(
                f"/api/v1/conversations/{conversation['id']}/messages",
                json={"client_request_id": "loop-sse-1", "text": "等待尾部", "file_ids": []},
            )
            assert submitted.status_code == 202, submitted.text
            run_id = submitted.json()["run_id"]
            await asyncio.wait_for(GatedTailRuntime.started.wait(), 10)

            slow = SlowDatabase(service.db, seconds=0.04)
            with slow:
                async with LoopProbe() as probe:
                    stream_task = asyncio.create_task(
                        trial_smoke.stream_run_events(client, run_id, timeout=20.0)
                    )
                    # Several SSE poll iterations (public_events + require) run
                    # while the run is still non-terminal.
                    await asyncio.sleep(0.9)
                    GatedTailRuntime.release.set()
                    stream = await asyncio.wait_for(stream_task, 25)

                slow.assert_all_off_loop()
                assert len(slow.calls) >= 4, slow.calls
                assert probe.max_gap < 0.25, f"SSE polling stalled the loop for {probe.max_gap:.3f}s"
                assert probe.percentile(0.95) < 0.10, probe.percentile(0.95)

            assert stream["connections"] == 1, "SSE reconnect instead of same-connection tail"
            terminal = _assert_terminal_ordering(stream)
            assert terminal["status"] == "succeeded", terminal
            assert "message.delta" in trial_smoke.event_types(stream)
    finally:
        GatedTailRuntime.release.set()
        await server.stop()


async def _scenario_concurrent_submissions(tmp_path: Path) -> None:
    immediate = 12
    server = await trial_smoke.SyntheticTrialServer.start(
        tmp_path / "loop-concurrent",
        max_active_sessions=16,
        idle_seconds=60,
        runtime_factory=ImmediateRuntime,
    )
    server.create_admin()
    service = server.app.state.service
    try:
        async with trial_smoke.make_client(server.base_url, timeout=60.0, max_connections=32) as client:
            await _login(client)
            assistant = await _assistant(client)
            conversations = [
                await _conversation(client, assistant, f"并发隔离 {index}")
                for index in range(immediate)
            ]

            async def submit(index: int) -> tuple[str, int]:
                response = await client.post(
                    f"/api/v1/conversations/{conversations[index]['id']}/messages",
                    json={"client_request_id": f"loop-concurrent-{index}", "text": "并发提交", "file_ids": []},
                )
                return response.status_code, index, response.json().get("run_id"), response.text

            slow = SlowDatabase(service.db, seconds=0.02)
            with slow:
                async with LoopProbe() as probe:
                    started = time.perf_counter()
                    responses = await asyncio.wait_for(
                        asyncio.gather(*(submit(index) for index in range(immediate))),
                        timeout=30,
                    )
                    elapsed = time.perf_counter() - started
                slow.assert_all_off_loop()
                assert elapsed < 20, f"submissions queued for {elapsed:.1f}s"
                assert probe.max_gap < 0.30, f"concurrent submission stalled the loop for {probe.max_gap:.3f}s"

            run_ids = []
            for status, index, run_id, text in responses:
                assert status == 202, (index, status, text)
                assert run_id, (index, text)
                run_ids.append(run_id)
            assert len(set(run_ids)) == immediate

            # Every accepted run must leave the queue and finish; nothing may
            # be stranded behind the offloaded writes.
            runs = await asyncio.gather(
                *(trial_smoke.wait_run(client, run_id, timeout=60.0) for run_id in run_ids)
            )
            assert {run["status"] for run in runs} == {"succeeded"}

            active = service.db.one(
                "SELECT COUNT(*) n FROM conversations WHERE id IN (%s) AND active_run_id IS NOT NULL"
                % ",".join("?" for _ in conversations),
                tuple(item["id"] for item in conversations),
            )["n"]
            assert active == 0, "a conversation retained active_run_id after completion"
    finally:
        await server.stop()


async def _scenario_cancel_idempotency_and_shutdown(tmp_path: Path) -> None:
    GatedCancelRuntime.reset()
    server = await trial_smoke.SyntheticTrialServer.start(
        tmp_path / "loop-cancel",
        max_active_sessions=2,
        idle_seconds=60,
        runtime_factory=GatedCancelRuntime,
    )
    server.create_admin()
    service = server.app.state.service
    manager = server.app.state.manager
    try:
        async with trial_smoke.make_client(server.base_url, timeout=30.0, max_connections=16) as client:
            await _login(client)
            assistant = await _assistant(client)
            conversation = await _conversation(client, assistant, "取消幂等")
            first = await client.post(
                f"/api/v1/conversations/{conversation['id']}/messages",
                json={"client_request_id": "loop-cancel-1", "text": "等待取消", "file_ids": []},
            )
            assert first.status_code == 202, first.text
            run_id = first.json()["run_id"]
            await asyncio.wait_for(GatedCancelRuntime.started.wait(), 10)

            duplicate = await client.post(
                f"/api/v1/conversations/{conversation['id']}/messages",
                json={"client_request_id": "loop-cancel-1", "text": "等待取消", "file_ids": []},
            )
            assert duplicate.status_code == 202, duplicate.text
            assert duplicate.json()["run_id"] == run_id
            assert service.db.one(
                "SELECT COUNT(*) n FROM runs WHERE conversation_id=?", (conversation["id"],)
            )["n"] == 1
            assert len(service.db.all(
                "SELECT id FROM messages WHERE conversation_id=? ORDER BY created_at,rowid", (conversation["id"],)
            )) == 2

            slow = SlowDatabase(service.db, seconds=0.03)
            with slow:
                async with LoopProbe() as probe:
                    cancelled = await client.post(f"/api/v1/runs/{run_id}/cancel")
                assert cancelled.status_code == 200, cancelled.text
                # A second cancel is a no-op, not a second terminal transition.
                again = await client.post(f"/api/v1/runs/{run_id}/cancel")
                assert again.status_code == 200, again.text
            slow.assert_all_off_loop()
            assert probe.max_gap < 0.25, f"cancel stalled the loop for {probe.max_gap:.3f}s"

            run = await trial_smoke.wait_run(client, run_id, timeout=20.0)
            assert run["status"] == "cancelled", run
            assert service.db.one(
                "SELECT COUNT(*) n FROM events WHERE run_id=? AND type='run.completed'", (run_id,)
            )["n"] == 1
            assert service.db.one(
                "SELECT active_run_id FROM conversations WHERE id=?", (conversation["id"],)
            )["active_run_id"] is None

            stream = await trial_smoke.stream_run_events(client, run_id, timeout=15.0)
            terminal = _assert_terminal_ordering(stream)
            assert terminal["status"] == "cancelled", terminal

            # Replaying the same client_request_id after the terminal state
            # still returns the original run and never creates new rows.
            replay = await client.post(
                f"/api/v1/conversations/{conversation['id']}/messages",
                json={"client_request_id": "loop-cancel-1", "text": "等待取消", "file_ids": []},
            )
            assert replay.status_code == 409 or replay.json()["run_id"] == run_id
            assert service.db.one(
                "SELECT COUNT(*) n FROM runs WHERE conversation_id=?", (conversation["id"],)
            )["n"] == 1
    finally:
        GatedCancelRuntime.release.set()
        await server.stop()

    # Shutdown waited for every actor and closed the database without leaving
    # queued work behind.
    assert manager.actors == {}, manager.actors


async def _scenario_prepare_cancelled_before_submit(tmp_path: Path) -> None:
    """A cancel between the prepare commit and the submit must not strand it.

    ``prepare_run`` durably claims ``conversations.active_run_id``; the
    shielded prepare+submit unit still delivers the run after the caller is
    cancelled, so the run leaves ``queued`` and the conversation is released.
    """
    server = await trial_smoke.SyntheticTrialServer.start(
        tmp_path / "loop-cancel-window",
        max_active_sessions=4,
        idle_seconds=60,
        runtime_factory=ImmediateRuntime,
    )
    server.create_admin()
    service = server.app.state.service
    manager = server.app.state.manager
    try:
        async with trial_smoke.make_client(server.base_url, timeout=30.0, max_connections=8) as client:
            await _login(client)
            assistant = await _assistant(client)
            conversation = await _conversation(client, assistant, "取消窗口")
            user = service.db.one("SELECT * FROM users WHERE username='synthetic-admin'")

            original = manager.ensure_submitted
            entered = asyncio.Event()
            release = asyncio.Event()

            async def gated(run_id):
                entered.set()
                await release.wait()
                await original(run_id)

            manager.ensure_submitted = gated
            try:
                task = asyncio.create_task(
                    service.prepare_and_submit_async(
                        conversation["id"],
                        {"client_request_id": "cancel-window-1", "text": "取消窗口", "file_ids": [], "skill_ids": []},
                        user,
                    )
                )
                await asyncio.wait_for(entered.wait(), 10)
                # The prepare transaction has committed and claimed the
                # conversation; cancel exactly in the submit window.
                committed = service.db.one(
                    "SELECT * FROM runs WHERE client_request_id='cancel-window-1'"
                )
                assert committed is not None and committed["status"] == "queued"
                task.cancel()
                await asyncio.sleep(0)
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                manager.ensure_submitted = original

            run = await trial_smoke.wait_run(client, committed["id"], timeout=20.0)
            assert run["status"] == "succeeded", run
            assert service.db.one(
                "SELECT active_run_id FROM conversations WHERE id=?", (conversation["id"],)
            )["active_run_id"] is None
            assert service.db.one(
                "SELECT COUNT(*) n FROM events WHERE run_id=? AND type='run.completed'",
                (committed["id"],),
            )["n"] == 1
    finally:
        await server.stop()


async def _scenario_retry_redelivers_orphaned_run(tmp_path: Path) -> None:
    """A committed but undelivered run is delivered by a duplicate request.

    Reproduces the F1 state directly: ``runs`` exists and is ``queued`` with
    ``active_run_id`` claimed, but no actor ever received it.  Retrying the
    same ``client_request_id`` must enqueue it exactly once instead of only
    returning the original row while the conversation stays busy.
    """
    GatedTailRuntime.reset()
    server = await trial_smoke.SyntheticTrialServer.start(
        tmp_path / "loop-redelivery",
        max_active_sessions=4,
        idle_seconds=60,
        runtime_factory=GatedTailRuntime,
    )
    server.create_admin()
    service = server.app.state.service
    manager = server.app.state.manager
    try:
        async with trial_smoke.make_client(server.base_url, timeout=30.0, max_connections=8) as client:
            await _login(client)
            assistant = await _assistant(client)
            conversation = await _conversation(client, assistant, "重投递")
            user = service.db.one("SELECT * FROM users WHERE username='synthetic-admin'")
            payload = {"client_request_id": "orphan-1", "text": "孤儿运行", "file_ids": [], "skill_ids": []}

            # Old-process edge: committed, never submitted.
            result, created = service.prepare_run(conversation["id"], payload, user)
            assert created is True
            run_id = result["run_id"]
            assert service.db.one("SELECT status FROM runs WHERE id=?", (run_id,))["status"] == "queued"

            retry = await client.post(
                f"/api/v1/conversations/{conversation['id']}/messages",
                json={"client_request_id": "orphan-1", "text": "孤儿运行", "file_ids": []},
            )
            assert retry.status_code == 202, retry.text
            assert retry.json()["run_id"] == run_id
            await asyncio.wait_for(GatedTailRuntime.started.wait(), 10)

            # A second retry while the run is in flight must not enqueue it
            # again; the actor's dedupe covers both queued and current runs.
            second = await client.post(
                f"/api/v1/conversations/{conversation['id']}/messages",
                json={"client_request_id": "orphan-1", "text": "孤儿运行", "file_ids": []},
            )
            assert second.status_code == 202, second.text
            assert second.json()["run_id"] == run_id
            await manager.ensure_submitted(run_id)

            GatedTailRuntime.release.set()
            run = await trial_smoke.wait_run(client, run_id, timeout=20.0)
            assert run["status"] == "succeeded", run

            assert service.db.one(
                "SELECT COUNT(*) n FROM runs WHERE conversation_id=?", (conversation["id"],)
            )["n"] == 1
            assert service.db.one(
                "SELECT COUNT(*) n FROM events WHERE run_id=? AND type='run.completed'", (run_id,)
            )["n"] == 1
            messages = await trial_smoke.request_json(
                client, "GET", f"/api/v1/conversations/{conversation['id']}/messages"
            )
            # Exactly one execution: the single delta appears once.
            assert messages[-1]["text"] == "尾部隔离验证", messages[-1]["text"]
            assert service.db.one(
                "SELECT active_run_id FROM conversations WHERE id=?", (conversation["id"],)
            )["active_run_id"] is None
    finally:
        GatedTailRuntime.release.set()
        await server.stop()


def test_message_send_and_actor_emit_leave_the_event_loop(tmp_path):
    asyncio.run(_scenario_prepare_run_and_emit(tmp_path))


def test_sse_polling_reads_leave_the_event_loop_and_keep_the_tail(tmp_path):
    asyncio.run(_scenario_sse_polling(tmp_path))


def test_concurrent_submissions_do_not_block_or_queue_forever(tmp_path):
    asyncio.run(_scenario_concurrent_submissions(tmp_path))


def test_cancel_idempotency_ordering_and_shutdown_survive_offloading(tmp_path):
    asyncio.run(_scenario_cancel_idempotency_and_shutdown(tmp_path))


def test_cancellation_between_prepare_and_submit_still_delivers_the_run(tmp_path):
    asyncio.run(_scenario_prepare_cancelled_before_submit(tmp_path))


def test_retry_redelivers_an_orphaned_queued_run_exactly_once(tmp_path):
    asyncio.run(_scenario_retry_redelivers_orphaned_run(tmp_path))


def test_offloaded_write_is_not_abandoned_when_its_awaiter_is_cancelled():
    async def scenario() -> None:
        state = {"finished": False, "thread": None}

        def write() -> None:
            time.sleep(0.05)
            state["finished"] = True
            state["thread"] = threading.get_ident()

        task = asyncio.create_task(offload_write(write))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The worker cannot be interrupted; the helper waited for it so a
        # terminal run.completed can never overtake an in-flight write.
        assert state["finished"] is True
        assert state["thread"] != threading.get_ident()

    asyncio.run(scenario())


def test_offloaded_write_propagates_a_worker_failure():
    async def scenario() -> None:
        def boom() -> None:
            raise ValueError("boom-normal")

        with pytest.raises(ValueError, match="boom-normal"):
            await offload_write(boom)

    asyncio.run(scenario())


def test_offloaded_write_cancel_path_surfaces_a_worker_failure():
    """F4: cancelling the waiter must not hide a failed transaction."""

    async def scenario() -> None:
        def boom() -> None:
            time.sleep(0.08)
            raise ValueError("boom-cancelled")

        task = asyncio.create_task(offload_write(boom))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(ValueError, match="boom-cancelled"):
            await task

    asyncio.run(scenario())
