from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from app.artifacts import image_generation_capability, write_xlsx, xlsx_bytes
from app.config import Settings
from app.main import create_app
from app.security import AppError
from test_api import PASSWORD, login


def test_xlsx_export_is_valid_and_never_evaluates_formula_text(tmp_path):
    payload = xlsx_bytes([{"name": "质量记录", "columns": ["物料", "备注"], "rows": [["A-01", "=HYPERLINK(\"http://evil\")"], ["B-02", "@cmd"]]}])
    with ZipFile(BytesIO(payload)) as archive:
        names = archive.namelist()
        assert "xl/worksheets/sheet1.xml" in names
        xml = archive.read("xl/worksheets/sheet1.xml")
        ET.fromstring(xml)
        text = xml.decode()
        assert "'=" in text and "'@cmd" in text
        assert "<f>" not in text


def test_xlsx_export_bounds_and_writes_below_artifacts(tmp_path):
    path, size = write_xlsx(tmp_path / "conversation", "质量报告", [{"name": "数据", "rows": [[1, 2]]}])
    assert path.name == "质量报告.xlsx"
    assert path.parent.name == "artifacts"
    assert size == path.stat().st_size and path.stat().st_size > 0
    with pytest.raises(AppError) as error:
        xlsx_bytes([{"name": "数据", "rows": [["x"]] * 5001}])
    assert error.value.code == "ARTIFACT_TOO_MANY_ROWS"


def test_image_generation_is_explicitly_unavailable_without_authorized_adapter():
    settings = SimpleNamespace(image_generation_enabled=False, image_model_id="", image_generator_url="")
    catalog = SimpleNamespace(models={})
    value = image_generation_capability(settings, catalog)
    assert value["available"] is False
    assert value["status"] == "pending_authorization"
    assert "不会伪造" in value["reason"]


def test_xlsx_api_creates_user_owned_download_and_keeps_image_default_denied(tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "data", provider_settings="", origins=("http://testserver",)))
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)
        response = client.post("/api/v1/artifacts/xlsx", json={
            "filename": "质量导出.xlsx",
            "sheets": [{"name": "质量", "columns": ["批次", "合格率"], "rows": [["L01", "98.2%"]]}],
        })
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["format"] == "xlsx"
        download = client.get(payload["download_path"])
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/octet-stream"
        assert "attachment" in download.headers["content-disposition"]
        capability = client.get("/api/v1/artifacts/image-capability")
        assert capability.status_code == 200 and capability.json()["available"] is False
        image = client.post("/api/v1/artifacts/image", json={"prompt": "工业质量看板"})
        assert image.status_code == 503 and image.json()["error"]["code"] == "IMAGE_MODEL_UNAVAILABLE"
