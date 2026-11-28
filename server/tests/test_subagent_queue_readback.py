import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from test_api import FakeRuntime
from test_subagents import make_parent


@pytest.mark.parametrize("outcome", ["finish", "timeout", "cancel"])
def test_readback_while_harness_child_waits_for_a_worker(tmp_path, monkeypatch, outcome):
    settings = Settings(data_dir=tmp_path, provider_settings="", max_active_sessions=4,
                        harness_source_dir="fixture", harness_node_path="fixture")
    app = create_app(settings, runtime_factory=FakeRuntime)
    with TestClient(app):
        service, owner, run = make_parent(app)
        plans = service.subagents
        run_id = run["run_id"]
        service.db.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
        parent = SimpleNamespace(accepting_subagent_plans=True, mounted_tools={"delegate_tasks"},
                                 bridge=SimpleNamespace(business_tools={}))
        monkeypatch.setattr(plans, "harness_parent", lambda run: (None, parent))
        plan = plans.create_plan(run_id, owner["id"], ["first", "second"], "harness")
        first_id, second_id = (row["child_id"] for row in plan["children"])

        async def exercise():
            entered = asyncio.Event()
            waiting = asyncio.Event()
            release = asyncio.Event()
            received = []

            class OneWorker(asyncio.Semaphore):
                async def acquire(self):
                    if self.locked():
                        waiting.set()
                    return await super().acquire()

            async def executor(service, child, task, output_limit):
                received.append(child["child_id"])
                if child["child_id"] == first_id:
                    entered.set()
                    await release.wait()
                return {"output": task["task"] + " result"}

            monkeypatch.setattr(plans, "_worker_capacity", OneWorker(1))
            monkeypatch.setattr("app.harness_child.execute_harness_child", executor)
            if outcome == "timeout":
                monkeypatch.setattr(plans, "harness_child_timeout", lambda: 0.25)
            job = plans.start_plan(run_id, owner["id"])
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await asyncio.wait_for(waiting.wait(), 2)
                first, second = plans.list_children(run_id, owner["id"])
                assert (first["status"], second["status"]) == ("running", "queued")
                assert second["started_at"] is None
                assert not any(e["type"] == "subagent.started" and e["data"]["child_id"] == second_id
                               for e in service.db.events(run_id, 0))
                if outcome == "cancel":
                    await plans.cancel_plan(run_id, owner["id"])
            finally:
                if outcome != "timeout":
                    release.set()
                await asyncio.wait_for(job, 3)

            first, second = plans.list_children(run_id, owner["id"])
            if outcome == "cancel":
                assert [first["status"], second["status"]] == ["cancelled", "cancelled"]
                assert received == [first_id]
                assert second["started_at"] is None
            else:
                assert received == [first_id, second_id]
                assert second["status"] == "succeeded"
                assert second["result"]["output"] == "second result"
                if outcome == "timeout":
                    assert (first["status"], first["stop_reason"]) == ("interrupted", "timeout")
                else:
                    assert first["result"]["output"] == "first result"

        asyncio.run(exercise())
