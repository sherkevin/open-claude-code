"""Knowledge-service cancellation; process termination has its own real tests."""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from app.hipporag import HippoRAGError
from app.knowledge import KnowledgeService
from app.security import AppError
from test_hipporag_official_search import _run, system


class WaitingBackend:
    supports_cancellation = True

    def __init__(self):
        self.started = threading.Event()
        self.stopped = threading.Event()

    def search(self, query, *, rows, limit, cancel_event):
        self.started.set()
        try:
            end = time.monotonic() + 5
            while time.monotonic() < end:
                if cancel_event.is_set():
                    raise HippoRAGError("cancelled")
                time.sleep(.01)
            raise AssertionError("cancellation was not delivered")
        finally:
            self.stopped.set()


@pytest.mark.parametrize("error", ["timeout", "worker_failed", "worker_protocol"])
def test_worker_failure_has_explicit_lite_fallback(system, error):
    _, service, users, kb, _ = system

    class Backend:
        supports_cancellation = True

        def search(self, *args, **kwargs):
            raise HippoRAGError(error)

    service.knowledge.hipporag_backend = Backend()
    result = service.knowledge.search([kb["id"]], users["owner"], "P-101", mode="hipporag")
    assert result["retrieval"]["algorithm"] == "hipporag2-lite"
    assert result["retrieval"]["backend"] == "lite"
    assert result["retrieval"]["fallback"] == error
    assert result["results"]


def test_async_run_cancel_waits_for_backend_and_registers_no_sources(system):
    _, service, users, kb, _ = system
    assistant, _, run_id = _run(service, users["owner"], kb)
    backend = WaitingBackend()
    service.knowledge.hipporag_backend = backend

    async def scenario():
        task = asyncio.create_task(service.knowledge.search_for_run(users["owner"], assistant, run_id, "P-101"))
        assert await asyncio.to_thread(backend.started.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backend.stopped.is_set()

    asyncio.run(scenario())
    assert service.db.one("SELECT COUNT(*) n FROM evidence_sources WHERE run_id=?", (run_id,))["n"] == 0


@pytest.mark.parametrize("change", ["run_cancelled", "user_disabled", "kb_unbound"])
def test_waiting_backend_observes_revocation_without_explicit_async_cancel(system, change):
    _, service, users, kb, _ = system
    assistant, _, run_id = _run(service, users["owner"], kb)
    backend = WaitingBackend()
    service.knowledge.hipporag_backend = backend

    async def scenario():
        task = asyncio.create_task(service.knowledge.search_for_run(users["owner"], assistant, run_id, "P-101"))
        assert await asyncio.to_thread(backend.started.wait, 3)
        if change == "run_cancelled":
            service.db.execute("UPDATE runs SET status='cancelled' WHERE id=?", (run_id,))
        elif change == "user_disabled":
            service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (users["owner"]["id"],))
        else:
            service.save_assistant({"knowledge_base_ids": []}, assistant["id"], user=users["owner"])
        with pytest.raises(AppError):
            await asyncio.wait_for(task, 3)
        assert backend.stopped.is_set()

    asyncio.run(scenario())
    assert service.db.one("SELECT COUNT(*) n FROM evidence_sources WHERE run_id=?", (run_id,))["n"] == 0


def test_browser_disconnect_cancels_retrieval_instead_of_falling_back(system):
    _, service, users, kb, _ = system
    backend = WaitingBackend()
    service.knowledge.hipporag_backend = backend

    async def disconnected():
        return backend.started.is_set()

    async def scenario():
        with pytest.raises(AppError) as raised:
            await service.knowledge.search_async([kb["id"]], users["owner"], "P-101", mode="hipporag", disconnected=disconnected)
        assert raised.value.code == "KNOWLEDGE_SEARCH_CANCELLED"
        assert backend.stopped.is_set()

    asyncio.run(scenario())


def test_cancelled_call_does_not_dispatch_a_backend(system):
    _, service, users, kb, _ = system
    backend = WaitingBackend()
    service.knowledge.hipporag_backend = backend
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(AppError) as raised:
        service.knowledge.search([kb["id"]], users["owner"], "P-101", mode="hipporag", cancel_event=cancelled)
    assert raised.value.code == "KNOWLEDGE_SEARCH_CANCELLED"
    assert not backend.started.is_set()


def test_caller_cancel_during_disconnect_watcher_cleanup_is_not_swallowed():
    async def scenario():
        watcher_started = threading.Event()
        cleaning = asyncio.Event()

        def search(*args, **kwargs):
            assert watcher_started.wait(3)
            return {"synthetic": "complete"}

        async def disconnected():
            watcher_started.set()
            try:
                await asyncio.sleep(60)
            finally:
                cleaning.set()
                await asyncio.sleep(60)

        service = SimpleNamespace(search=search, hipporag_backend=None)
        task = asyncio.create_task(KnowledgeService.search_async(service, disconnected=disconnected))
        await asyncio.wait_for(cleaning.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
