"""Optional integration tests for the actual pinned upstream algorithms."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify_hipporag_official.py"
SOURCE = ROOT / ".runtime/third_party/HippoRAG"
PYTHON = ROOT / ".runtime/hipporag-env/bin/python"
ENVIRONMENT_MANIFEST = ROOT / ".runtime/hipporag-env/installation.json"


def load_script():
    spec = importlib.util.spec_from_file_location("hipporag_official_verifier", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_source_fails_before_importing_optional_dependencies(tmp_path):
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--source", str(tmp_path / "absent"),
         "--temp-root", str(tmp_path / "unused")], capture_output=True, text=True, timeout=20,
    )
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert report["status"] == "FAIL"
    assert report["external_model_calls"] == 0
    assert not (tmp_path / "unused").exists()


def test_synthetic_duplicate_keeps_distinct_document_identity():
    script = load_script()
    rows = script.synthetic_rows(["synthetic text", "other synthetic text"])
    assert rows[0]["text"] == rows[-1]["text"]
    assert rows[0]["sha256"] == rows[-1]["sha256"]
    assert rows[0]["id"] != rows[-1]["id"]
    assert rows[0]["document_id"] != rows[-1]["document_id"]
    assert set(script.adapter_identity()) == {
        "upstream_commit", "parser_digest", "extraction_digest", "embedding_digest",
        "chunking_digest", "acl_digest",
    }


@pytest.fixture
def license_copies(tmp_path, monkeypatch):
    script = load_script()
    original = script.BUNDLED_LICENSE.read_bytes()
    source = tmp_path / "source"
    source.mkdir()
    upstream, bundled = source / "LICENSE", tmp_path / "LICENSE.HippoRAG"
    upstream.write_bytes(original)
    bundled.write_bytes(original)
    monkeypatch.setattr(script, "BUNDLED_LICENSE", bundled)
    return script, source, {"upstream": upstream, "bundled": bundled}


def test_original_upstream_and_bundled_license_are_verified(license_copies):
    script, source, _ = license_copies
    assert script.license_identity(source) == {
        "license": "MIT",
        "license_sha256": "93b43d692b033b76129504448695bfe76ef22d18b11e51352bfab4b5622e5aaa",
    }


@pytest.mark.parametrize("copy", ["upstream", "bundled"])
@pytest.mark.parametrize("defect", ["missing", "modified", "symlink", "oversized"])
def test_license_defect_is_rejected_before_source_or_model_import(license_copies, monkeypatch, copy, defect):
    script, source, paths = license_copies
    path = paths[copy]
    if defect == "missing":
        path.unlink()
    elif defect == "modified":
        path.write_bytes(path.read_bytes() + b"\nModified copyright notice\n")
    elif defect == "symlink":
        target = path.with_name(path.name + ".original")
        path.rename(target)
        path.symlink_to(target)
    else:
        path.write_bytes(b"x" * 65_537)

    def unexpected_source_access(*args, **kwargs):
        pytest.fail("License verification must precede upstream source and optional imports")

    monkeypatch.setattr(script.subprocess, "check_output", unexpected_source_access)
    with pytest.raises(RuntimeError, match="HippoRAG LICENSE"):
        script.source_identity(source)


def test_shared_dependencies_use_install_manifest_not_callers_home(tmp_path, monkeypatch):
    environment = tmp_path / "environment"
    (environment / "bin").mkdir(parents=True)
    installed_site = tmp_path / "installed-packages"
    installed_site.mkdir()
    (environment / "installation.json").write_text(json.dumps({"shared_site": str(installed_site)}))
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-empty-home"))
    assert load_script().shared_dependency_site(environment / "bin/python") == installed_site


def test_explicit_shared_dependency_directory_does_not_need_a_manifest(tmp_path):
    assert load_script().shared_dependency_site(tmp_path / "absent/bin/python", tmp_path) == tmp_path


@pytest.mark.parametrize("configured", [None, "relative/packages", "/not/a/real/hipporag/dependency/directory"])
def test_invalid_manifest_shared_dependency_directory_fails_closed(tmp_path, configured):
    (tmp_path / "installation.json").write_text(json.dumps({"shared_site": configured}))
    with pytest.raises(RuntimeError):
        load_script().shared_dependency_site(tmp_path / "bin/python")


@pytest.mark.skipif(not (SOURCE / ".git").exists(), reason="Pinned upstream checkout is absent")
def test_unreviewed_upstream_commit_is_rejected():
    with pytest.raises(RuntimeError, match="reviewed pin"):
        load_script().source_identity(SOURCE, "0" * 40)


@pytest.mark.skipif(not PYTHON.exists() or not ENVIRONMENT_MANIFEST.exists() or not (SOURCE / ".git").exists(),
                    reason="Optional isolated HippoRAG environment or source is absent")
def test_real_official_pipeline_and_product_adapter(tmp_path):
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--temp-root", str(tmp_path)],
        capture_output=True, text=True, timeout=220,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "PASS"
    assert report["scope"] == "official-algorithms-synthetic-models"
    assert report["external_model_calls"] == 0
    assert report["external_io_attempts"] == {}
    assert report["graph"]["seed_count"] > 0
    assert report["graph"]["synonym_edges"] > 0
    assert report["official_calls_observed"]["official_ppr"] >= 3
    assert report["official_calls_observed"]["official_ner_parser"] == 10
    assert report["official_calls_observed"]["official_triple_parser"] == 10
    assert report["synthetic_llm_calls"] == {"ner": 10, "triples": 10, "fact_filter": 3}
    assert report["synthetic_embedding_calls"] == 9
    assert set(report["synthetic_model_stages"]) == {
        "index_and_retrieve", "reopened_retrieve", "adapter_index_and_retrieve",
    }
    assert report["synthetic_model_stages"]["reopened_retrieve"]["llm_calls"] == {"fact_filter": 1}
    assert report["adapter"]["source_count"] == 6
    assert report["adapter"]["unique_text_count"] == 5
    assert report["adapter"]["returned_count"] == 6
    assert report["completed_at"]
    assert len(report["source"]["source_sha256"]) == 64
    assert report["source"]["python_files"] == 63
    assert report["source"]["license"] == "MIT"
    assert report["source"]["license_sha256"] == load_script().PINNED_LICENSE_SHA256
    assert len(report["checks"]) == 6
    assert all(check["status"] == "PASS" for check in report["checks"])


@pytest.mark.skipif(not PYTHON.exists() or not ENVIRONMENT_MANIFEST.exists() or not (SOURCE / ".git").exists(),
                    reason="Optional isolated HippoRAG environment or source is absent")
def test_real_official_pipeline_with_empty_environment_and_home(tmp_path):
    home, temporary = tmp_path / "empty-home", tmp_path / "tmp"
    home.mkdir()
    temporary.mkdir()
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--temp-root", str(temporary)],
        env={"PATH": "/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(temporary),
             "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache")},
        capture_output=True, text=True, timeout=220,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    manifest = json.loads(ENVIRONMENT_MANIFEST.read_text())
    assert report["status"] == "PASS"
    assert report["shared_dependency_site"] == str(Path(manifest["shared_site"]).resolve())
    assert report["official_calls_observed"]["official_ppr"] == 3
    assert report["synthetic_llm_calls"] == {"ner": 10, "triples": 10, "fact_filter": 3}
    assert report["external_model_calls"] == 0
    assert report["external_io_attempts"] == {}
