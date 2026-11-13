from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_workflow_api_is_explicitly_disabled_by_default(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",)))
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "管理员", "password": "workflow-disabled-password", "role": "admin"})
        login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "workflow-disabled-password"})
        assert login.status_code == 200, login.text
        checks = [
            ("GET", "/api/v1/workflow-templates"),
            ("GET", "/api/v1/assistants/assistant-1/workflows"),
            ("POST", "/api/v1/assistants/assistant-1/workflows"),
            ("PATCH", "/api/v1/workflows/workflow-1/draft"),
            ("POST", "/api/v1/workflows/workflow-1/publish"),
            ("POST", "/api/v1/workflows/workflow-1/runs"),
            ("GET", "/api/v1/workflow-runs/run-1"),
            ("POST", "/api/v1/workflow-runs/run-1/approvals/node-1/decision"),
        ]
        for method, path in checks:
            response = client.request(method, path, json={})
            assert response.status_code == 410, (method, path, response.text)
            assert response.json()["error"] == {
                "code": "FEATURE_DISABLED",
                "message": "工作流功能已下线，当前版本请使用助手配置、会话、知识库和业务连接。",
            }
