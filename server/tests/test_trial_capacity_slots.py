"""Regression for the execution-slot admission accounting.

On Python 3.10 ``asyncio.wait_for(queue.get(), ...)`` runs ``queue.get()`` in a
helper task.  For one loop turn the actor therefore has an item that is no
longer in the queue and a ``current_run_id`` that is not set yet.  Treating
that window as idle admitted a fifth session and evicted an actor that still
executed its accepted run, so peak concurrency exceeded the configured slots.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.runtime import RunManager, SessionActor
from app.security import AppError


def _manager(sessions: int = 4) -> RunManager:
    async def close_subagents():
        return None

    service = SimpleNamespace(
        settings=SimpleNamespace(max_active_sessions=sessions, idle_seconds=60),
        subagents=SimpleNamespace(close=close_subagents),
    )
    return RunManager(service)


async def _cleanup(manager: RunManager) -> None:
    actors = list(manager.actors.values())
    for actor in actors:
        actor.queue.put_nowait(None)
    tasks = {actor.task for actor in actors} | set(manager.retiring)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def test_pending_handoff_window_is_not_treated_as_idle():
    async def scenario():
        manager = _manager()
        try:
            first = manager.actor_for("c1")
            second = manager.actor_for("c2")
            third = manager.actor_for("c3")
            fourth = manager.actor_for("c4")
            for actor in (first, second, third):
                actor.current_run_id = "run-" + actor.conversation_id
            # The helper task has taken c4's item off the queue, but the actor
            # loop has not resumed to mark the run yet.
            fourth.pending = 1

            with pytest.raises(AppError) as caught:
                manager.actor_for("c5")

            assert caught.value.code == "EXECUTION_CAPACITY"
            assert set(manager.actors) == {"c1", "c2", "c3", "c4"}
        finally:
            await _cleanup(manager)

    asyncio.run(scenario())


def test_idle_actor_frees_its_slot_without_orphaning_executions():
    async def scenario():
        manager = _manager()
        try:
            first = manager.actor_for("c1")
            second = manager.actor_for("c2")
            third = manager.actor_for("c3")
            idle = manager.actor_for("idle")
            for actor in (first, second, third):
                actor.current_run_id = "run-" + actor.conversation_id

            replacement = manager.actor_for("new")

            assert replacement.conversation_id == "new"
            assert "idle" not in manager.actors
            assert len(manager.actors) == 4
            # The evicted actor was genuinely idle, so it is asked to stop.
            # Leave the sentinel in its queue for the cleanup below.
            assert idle.queue.qsize() == 1
        finally:
            await _cleanup(manager)

    asyncio.run(scenario())


def test_closing_actor_rejects_same_conversation_until_native_runtime_closes():
    async def scenario():
        manager = _manager(sessions=1)
        try:
            first = manager.actor_for("c1")
            first.closing = True

            # The old actor still owns the native runtime for this
            # conversation.  Creating a replacement now would overlap two
            # native sessions, so admission must fail closed until it exits.
            with pytest.raises(AppError) as caught:
                manager.actor_for("c1")
            assert caught.value.code == "EXECUTION_CAPACITY"
            assert first.task in manager.retiring

            first.queue.put_nowait(None)
            await asyncio.gather(first.task, return_exceptions=True)

            replacement = manager.actor_for("c1")
            assert replacement is not first
            assert manager.actors["c1"] is replacement
        finally:
            await _cleanup(manager)

    asyncio.run(scenario())


def test_timeout_race_does_not_leave_queued_work():
    async def scenario():
        manager = _manager(sessions=1)
        manager.service.settings.idle_seconds = 0.05
        finished = []
        manager.finish = lambda run_id, status, *args, **kwargs: finished.append((run_id, status))
        manager.service.require = lambda table, item_id, *args, **kwargs: (
            {"status": "cancelling"} if table == "runs" else None
        )
        actor = manager.actor_for("c1")
        original = SessionActor.next_item.__get__(actor)
        calls = 0

        async def racing_timeout():
            nonlocal calls
            calls += 1
            if calls == 1:
                # The idle timer fires in the same loop turn in which this
                # submission arrives.  The actor must consume it rather than
                # break with the run still queued.
                actor.enqueue("run-race")
                raise asyncio.TimeoutError()
            return await original()

        actor.next_item = racing_timeout
        try:
            await asyncio.wait_for(actor.task, 2)
        finally:
            if not actor.task.done():
                actor.queue.put_nowait(None)
                await asyncio.gather(actor.task, return_exceptions=True)
        assert finished == [("run-race", "cancelled")]
        assert actor.pending == 0
        assert actor.queue.empty()

    asyncio.run(scenario())


def test_close_waits_for_retired_actor_before_database_shutdown():
    class SlowRuntime:
        def __init__(self):
            self.release = asyncio.Event()

        async def close(self):
            await self.release.wait()

    async def scenario():
        manager = _manager(sessions=2)
        first = manager.actor_for("a")
        slow = SlowRuntime()
        first.runtime = slow
        manager.actor_for("b")
        manager.actor_for("c")  # evicts the idle actor for "a"

        assert first.task in manager.retiring
        closing = asyncio.create_task(manager.close())
        await asyncio.sleep(0.05)
        assert not closing.done(), "close returned while a retired actor was still closing its runtime"

        slow.release.set()
        await asyncio.wait_for(closing, 2)
        assert first.task.done()
        assert not manager.retiring
        assert not manager.retiring_by_conversation

    asyncio.run(scenario())
