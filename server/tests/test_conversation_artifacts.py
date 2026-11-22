import json
from io import BytesIO
from zipfile import ZipFile

from fastapi.testclient import TestClient

from app.artifacts import xlsx_bytes
from app.db import dump
from app.main import create_app
from app.security import AppError
from test_api import FakeRuntime, PASSWORD, configured, conversation, login, poll, send


WORKBOOK = xlsx_bytes([{"name": "Fixture", "rows": [[42]]}])


class ArtifactRuntime(FakeRuntime):
    async def execute(self, run, message):
        directory = self.service.settings.data_dir / "workspaces" / self.conversation_id / "artifacts"
        directory.mkdir(parents=True, exist_ok=True)
        if message["text"] == "generate":
            (directory / "相同.xlsx").write_bytes(WORKBOOK)
        elif message["text"] == "partial":
            (directory / "失败文件.xlsx").write_bytes(WORKBOOK)
            raise AppError(502, "TEST_RUNTIME_FAILURE", "Partial fixture output")
        await super().execute(run, message)


def _run(client, conversation_id, case, number):
    accepted = send(client, conversation_id, case, f"artifact-fixture-{number}")
    run = poll(client, accepted["run_id"])
    messages = client.get(f"/api/v1/conversations/{conversation_id}/messages").json()
    output = next(message for message in messages if message["id"] == run["assistant_message_id"])
    events = client.app.state.service.db.events(run["id"], 0)
    return run, output["file_ids"], [event["data"]["id"] for event in events if event["type"] == "artifact.created"]


def test_each_turn_claims_only_its_own_artifacts(configured):
    app = create_app(configured, runtime_factory=ArtifactRuntime)
    with TestClient(app) as client:
        service = client.app.state.service
        service.create_user({"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"})
        login(client)
        cid = conversation(client)["id"]
        first, first_files, first_events = _run(client, cid, "generate", 1)
        second, second_files, second_events = _run(client, cid, "generate", 2)
        failed, failed_files, failed_events = _run(client, cid, "partial", 3)
        unrelated, unrelated_files, unrelated_events = _run(client, cid, "text only", 4)

        assert first["status"] == second["status"] == unrelated["status"] == "succeeded"
        assert failed["status"] == "failed"
        assert len(first_files) == 1
        assert first_files == first_events == second_files == second_events
        assert not failed_files and not failed_events and not unrelated_files and not unrelated_events
        assert client.get(f"/api/v1/files/{first_files[0]}/content").content == WORKBOOK


def test_conversation_export_is_complete_and_owner_scoped(configured):
    app = create_app(configured, runtime_factory=ArtifactRuntime)
    with TestClient(app) as client:
        service = client.app.state.service
        owner = service.create_user({"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"})
        service.create_user({"username": "member", "display_name": "另一个用户", "password": PASSWORD, "role": "member"})
        login(client)
        cid = conversation(client)["id"]
        upload = client.post("/api/v1/files", files={"file": ("引用资料.txt", b"original-file", "text/plain")})
        assert upload.status_code == 201
        file_id = upload.json()["id"]
        request = send(client, cid, "需要完整导出的中文问题", "export-fixture-1", files=[file_id])
        run = poll(client, request["run_id"])
        assert run["status"] == "succeeded"
        source = service.evidence.register(run["id"], "knowledge", "当前可访问资料", {"file": "引用资料.txt"},
                                           "可访问的来源摘要", resource_type="file", resource_id=file_id)
        service.db.execute("UPDATE messages SET citations=? WHERE id=?",
                           (dump([{"id": source["id"], "number": 1}]), run["assistant_message_id"]))
        service.db.event(run["id"], "tool.completed", {"tool_call_id": "receipt-1", "tool_name": "fixture",
                         "success": True, "arguments": {"authorization": "synthetic-secret"}})
        route = f"/api/v1/conversations/{cid}/export"
        markdown = client.get(route + "?format=markdown")
        assert markdown.status_code == 200
        assert all(label in markdown.json()["markdown"] for label in ("中文问题", "可访问的来源摘要", "工具 fixture · 成功"))
        response = client.get(route)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["content-disposition"] == f'attachment; filename="conversation-{cid}.zip"'
        with ZipFile(BytesIO(response.content)) as export:
            value = json.loads(export.read("conversation.json"))
            assert value["conversation"]["id"] == cid
            assert [item["role"] for item in value["messages"]] == ["user", "assistant"]
            attachment, = value["attachments"]
            assert export.read(attachment["archive_path"]) == b"original-file"
            assert attachment["archive_path"].encode() not in export.read("conversation.md")  # URI-encoded Markdown link
            assert b"attachments/0001-" in export.read("conversation.md")
            assert value["runs"][0]["sources"][0]["id"] == source["id"]
            assert value["runs"][0]["tools"][0]["tool_call_id"] == "receipt-1"
            assert b"synthetic-secret" not in export.read("conversation.json")
            assert "path" not in attachment

        service.db.execute("UPDATE files SET user_id=? WHERE id=?", ("revoked-owner", file_id))
        denied_markdown = client.get(route + "?format=markdown").json()["markdown"]
        assert "可访问的来源摘要" not in denied_markdown
        with ZipFile(BytesIO(client.get(route).content)) as export:
            assert set(export.namelist()) == {"conversation.md", "conversation.json"}
        service.db.execute("UPDATE files SET user_id=? WHERE id=?", (owner["id"], file_id))
        login(client, "member")
        assert client.get(route).status_code == 404


def test_trial_cannot_export_conversation(configured):
    configured.deployment_mode = "trial"
    app = create_app(configured, runtime_factory=ArtifactRuntime)
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"})
        login(client)
        assert client.get("/api/v1/conversations/unavailable/export").status_code == 403
