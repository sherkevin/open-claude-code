"""Deterministic regression for the SSE tail-loss race.

The events route reads an event snapshot and then, in a separate read, the
run's terminal status.  A run that finishes between those two reads used to
close the stream without the ``run.completed`` event, so a client on a healthy
loopback had to reconnect to discover the tail.

The test injects the interleaving directly: the first ``public_events`` call
returns the snapshot and only then marks the run succeeded, exactly as if the
actor had committed the terminal state in between.  The stream must still
deliver ``run.completed`` in the same HTTP connection.
"""
import asyncio
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import trial_smoke  # noqa: E402


class GatedTailRuntime:
    """Synthetic runtime parked in ``execute`` until the test releases it."""

    started = asyncio.Event()
    release = asyncio.Event()

    @classmethod
    def reset_counters(cls) -> None:
        cls.started = asyncio.Event()
        cls.release = asyncio.Event()

    def __init__(self, service, conversation_id, config, approve, emit):
        self.emit = emit

    async def connect(self) -> None:
        return None

    async def execute(self, run, message) -> None:
        type(self).started.set()
        await type(self).release.wait()
        await self.emit("message.delta", {"message_id": run["assistant_message_id"], "text": "尾部回归"})

    async def interrupt(self) -> None:
        type(self).release.set()

    async def close(self) -> None:
        return None


async def _scenario(tmp_path: Path) -> None:
    server = await trial_smoke.SyntheticTrialServer.start(
        tmp_path / "sse-tail",
        max_active_sessions=1,
        idle_seconds=60,
        runtime_factory=GatedTailRuntime,
    )
    server.create_admin()
    service = server.app.state.service
    manager = server.app.state.manager
    try:
        async with trial_smoke.make_client(server.base_url, timeout=10.0, max_connections=8) as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={"username": "synthetic-admin", "password": trial_smoke.PASSWORD},
            )
            assert login.status_code == 200
            assistant = await trial_smoke.request_json(
                client,
                "POST",
                "/api/v1/assistants",
                expected=(201,),
                json={
                    "name": "尾部回归助手",
                    "instructions": "仅用于学校云合成回归。",
                    "knowledge_base_ids": [],
                    "connections": [],
                    "web_search_enabled": False,
                },
            )
            conversation = await trial_smoke.request_json(
                client,
                "POST",
                "/api/v1/conversations",
                expected=(201,),
                json={"assistant_id": assistant["id"], "title": "尾部回归会话"},
            )
            submitted = await client.post(
                f"/api/v1/conversations/{conversation['id']}/messages",
                json={"client_request_id": "sse-tail-regression", "text": "尾部回归", "file_ids": []},
            )
            assert submitted.status_code == 202
            run_id = submitted.json()["run_id"]
            await asyncio.wait_for(GatedTailRuntime.started.wait(), 10)

            original = service.public_events
            finished = False

            def snapshot_then_finish(item_id, after):
                nonlocal finished
                events = original(item_id, after)
                if not finished:
                    finished = True
                    # The snapshot above is already fixed; the terminal state
                    # and its run.completed event land afterwards but before
                    # the route's separate status read.
                    manager.finish(run_id, "succeeded")
                return events

            service.public_events = snapshot_then_finish
            stream = await trial_smoke.stream_run_events(client, run_id, timeout=10.0)
            types = [event["sse_type"] for event in stream["events"]]
            assert types[-1] == "run.completed", types
            assert stream["connections"] == 1, "terminal event forced a reconnect"
            assert finished is True
    finally:
        GatedTailRuntime.release.set()
        await server.stop()


def test_sse_tail_survives_finish_between_snapshot_and_status_read(tmp_path):
    asyncio.run(_scenario(tmp_path))
