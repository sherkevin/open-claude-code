"""Synthetic fixture checks; network acceptance is the preview's verify command."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import stat

import pytest

spec = importlib.util.spec_from_file_location("business_loop_preview", Path(__file__).with_name("business_loop_preview.py"))
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    directory = tmp_path_factory.mktemp("business-loop-fixture")
    path = directory / "synthetic.sqlite3"
    preview.seed_database(path)
    return path


def test_fixture_materializes_100k_records_and_is_reusable(database):
    preview.seed_database(database)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == 100000
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


@pytest.mark.parametrize("size", [10000, 100000])
def test_aggregate_matches_independent_integer_arithmetic(database, size):
    result = preview.aggregate(database, "orders", size, "华北")
    selected = range(1, size + 1, 3)
    assert result["rows"] == [{"region": "华北", "record_count": len(selected),
        "amount": sum(10000 + (i % 997) * 123 for i in selected) / 100,
        "quantity": sum(1 + i % 40 for i in selected)}]
    assert result["synthetic"] is True and result["complete"] is True
    assert result["original_rows"] == size and result["returned_rows"] == 1
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 5000
    assert "无模型推理" in result["summary"]


def test_shipments_aggregate_by_customer_without_returning_raw_records(database):
    result = preview.aggregate(database, "shipments", 100000, "全部")
    assert len(result["rows"]) == 8
    assert sum(row["record_count"] for row in result["rows"]) == 80000
    assert result["scope"]["匹配原始记录数"] == 80000
    assert result["total_rows"] == 8
    assert result["sources"][0]["locator"]["synthetic"] is True


@pytest.mark.parametrize("changes", [{"dataset_rows": 999999}, {"dataset_rows": True},
    {"region": "华北' OR 1=1--"}, {"organization": "其他组织"}, {"kind": "delete"}])
def test_query_rejects_unknown_scope_or_unbounded_input(database, changes):
    with pytest.raises(ValueError):
        preview.aggregate(database, **({"kind": "orders"} | changes))


def test_credentials_are_private_stable_and_never_echoed(tmp_path, capsys):
    first = preview.load_credentials(tmp_path)
    assert preview.load_credentials(tmp_path) == first
    assert stat.S_IMODE((tmp_path / "credentials.json").stat().st_mode) == 0o600
    assert len(first["password"]) >= 32 and len(first["mcp_bearer"]) >= 32
    assert capsys.readouterr().out == ""


def test_credentials_and_output_refuse_symlinks(tmp_path):
    target = tmp_path / "outside.json"
    target.write_text("untouched")
    (tmp_path / "credentials.json").symlink_to(target)
    with pytest.raises(RuntimeError):
        preview.load_credentials(tmp_path)
    with pytest.raises(RuntimeError):
        preview.secure_json(tmp_path / "credentials.json", {"overwrite": True})
    assert target.read_text() == "untouched"


def test_observation_percentiles_are_nearest_rank_and_empty_is_unknown():
    assert preview.distribution([40, 10, 20, 30]) == {"samples": 4, "p50": 20, "p95": 40, "min": 10, "max": 40}
    assert preview.distribution([]) == {"samples": 0, "p50": None, "p95": None, "min": None, "max": None}
