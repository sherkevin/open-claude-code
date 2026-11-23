import json
from pathlib import Path

import pytest

from app.config import Settings
from app.model_catalog import ModelCatalog


@pytest.mark.parametrize(("model_id", "choices", "previous_default", "previous_effort"), [
    ("deepseek-v4.1-flash", "default light high max", "default", None),
    ("glm-5.3", "default light high max", "default", None),
    ("glm-5.3-flash", "light high max", "light", "low"),
    ("dano-v2.1", "light high max", "high", "high"),
    ("hy4", "default high", "default", None),
    ("kimi-k3.1-pro", "default light high max", "default", None),
    ("minimax-m3.1-flash", "light high max", "light", "low"),
])
def test_deployment_catalog_offers_runnable_levels_and_preserves_existing_selection(
    tmp_path, model_id, choices, previous_default, previous_effort
):
    example = Path(__file__).resolve().parents[1] / "model-catalog.example.json"
    template = json.loads(example.read_text())
    entry = next(row for row in template["models"] if row["id"] == model_id)
    entry.update(enabled=True, upstream_model="synthetic-model")
    config = tmp_path / "catalog.json"
    config.write_text(json.dumps({"default_model_id": model_id, "models": [entry]}))
    catalog = ModelCatalog(Settings(model_catalog=str(config), default_model_id="", provider_settings=""))
    public, = catalog.public()
    assert [level["id"] for level in public["reasoning_levels"]] == choices.split()
    expected = {"default": None, "light": "low", "high": "high", "max": "max"}
    for level in choices.split():
        assert catalog.select(model_id, level)["upstream_effort"] == expected[level]
    assert catalog.select(model_id)["reasoning_level"] == previous_default
    old = dict(model_id=model_id, reasoning_level=previous_default, upstream_model="synthetic-model",
               upstream_effort=previous_effort)
    assert catalog.validate_snapshot(old) == old
