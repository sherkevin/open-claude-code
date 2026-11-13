import threading
import time
from pathlib import Path

from PIL import Image

from app.vision import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, VisionRuntime


def _checkpoint(path: Path, dimensions: int = 2) -> Path:
    path.mkdir(parents=True)
    (path / "config.json").write_text(
        '{"projection_dim": %d, "model_type": "clip"}' % dimensions,
        encoding="utf-8",
    )
    # The runtime only inspects these files before loading.  The loading test
    # is deliberately kept separate so this fixture never downloads weights.
    (path / "model.safetensors").write_bytes(b"local-test-checkpoint")
    (path / "preprocessor_config.json").write_text("{}", encoding="utf-8")
    (path / "tokenizer.json").write_text("{}", encoding="utf-8")
    return path


def test_status_does_not_call_a_present_checkpoint_ready(tmp_path, monkeypatch):
    checkpoint = _checkpoint(tmp_path / "clip")
    monkeypatch.setenv("AGENT_KNOWLEDGE_VISION_MODEL_PATH", str(checkpoint))
    runtime = VisionRuntime(tmp_path / "knowledge")

    status = runtime.status()

    assert status["model_path_exists"] is True
    assert status["model_status"] == "configured"
    assert status["visual_embedding"] == "needs_model"
    assert status["visual_embedding_available"] is True
    assert status["embedding_dimensions"] == 2
    assert status["model_source"].startswith("local-clip:")
    assert str(checkpoint) not in str(status)


def test_explicit_missing_model_fails_closed_without_cache_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_VISION_MODEL_PATH", str(tmp_path / "missing"))
    runtime = VisionRuntime(tmp_path / "knowledge")
    image_path = tmp_path / "sample.png"
    Image.new("RGB", (16, 16), "white").save(image_path)

    assert runtime.status()["model_status"] == "missing"
    assert runtime.status()["visual_embedding"] == "needs_model"
    assert runtime.embed_image(image_path) is None
    assert runtime.status()["errors"]["visual"] is not None


def test_image_limits_are_checked_before_model_inference(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_VISION_MODEL_PATH", str(tmp_path / "missing"))
    runtime = VisionRuntime(tmp_path / "knowledge")
    too_large = tmp_path / "too-large.bin"
    too_large.write_bytes(b"x" * (MAX_IMAGE_BYTES + 1))

    assert runtime.embed_image(too_large) is None
    status = runtime.status()
    assert "too-large.bin" not in str(status)
    assert "image byte limit exceeded" not in str(status)


def test_pixel_limit_is_checked_even_when_compressed_image_is_small(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_VISION_MODEL_PATH", str(tmp_path / "missing"))
    runtime = VisionRuntime(tmp_path / "knowledge")
    image_path = tmp_path / "large-pixels.png"
    Image.new("RGB", (int(MAX_IMAGE_PIXELS**0.5) + 1, int(MAX_IMAGE_PIXELS**0.5) + 1), "white").save(image_path)

    assert runtime._check_image(image_path) is None
    assert "large-pixels.png" not in str(runtime.status())


def test_vector_dimensions_and_non_finite_values_fail_closed(tmp_path):
    runtime = VisionRuntime(tmp_path / "knowledge")

    class Vector:
        def __init__(self, values):
            self.values = values

        def detach(self):
            return self

        def float(self):
            return self

        def cpu(self):
            return self

        def reshape(self, *_shape):
            return self

        def tolist(self):
            return self.values

    assert runtime._validated_vector(Vector([1.0, 2.0, 3.0]), 2) is None
    assert runtime._validated_vector(Vector([1.0, 2.0]), 2) == [0.4472136, 0.89442719]
    assert runtime._validated_vector(Vector([float("nan"), 1.0]), 2) is None


def test_errors_are_type_only_and_do_not_echo_paths_or_credentials(tmp_path):
    runtime = VisionRuntime(tmp_path / "knowledge")
    runtime._record_clip_error(RuntimeError("/srv/private/model token=super-secret"))
    runtime._record_ocr_error(RuntimeError("/srv/private/ocr Authorization=Bearer secret"))

    errors = runtime.status()["errors"]
    assert errors["visual"] == "RuntimeError: 本地视觉模型不可用"
    assert errors["ocr"] == "RuntimeError: 本地 OCR 不可用"
    assert "/srv/private" not in str(errors)
    assert "super-secret" not in str(errors)
    assert "Bearer" not in str(errors)


def test_inference_lock_serializes_model_sessions(tmp_path):
    runtime = VisionRuntime(tmp_path / "knowledge")
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()

    def first():
        with runtime._inference_lock:
            entered.set()
            release.wait(timeout=2)

    def second():
        with runtime._inference_lock:
            second_entered.set()

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    assert entered.wait(timeout=1)
    second_thread.start()
    time.sleep(0.05)
    assert not second_entered.is_set()
    release.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)
    assert second_entered.is_set()
