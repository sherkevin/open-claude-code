"""Local visual indexing primitives for knowledge documents.

The visual path is deliberately opt-in and offline-only.  It can use a local
CLIP checkpoint for image embeddings and RapidOCR for text boxes.  If either
dependency/checkpoint is unavailable, callers receive an explicit unavailable
status rather than a fabricated semantic result.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from pathlib import Path
from typing import Any


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
DEFAULT_CLIP_MODEL = "openai/clip-vit-base-patch32"
# Keep visual inference bounded independently from the HTTP upload limit.  The
# limit protects model memory when callers invoke this runtime directly (for
# example, during re-indexing or a background task).
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_SIDE = 20_000


def _normalise(values: list[float]) -> list[float]:
    total = sum(value * value for value in values) ** 0.5 or 1.0
    return [round(value / total, 8) for value in values]


class VisionRuntime:
    """Lazy local visual runtime with truthful capability reporting.

    Loading heavyweight models is deferred until the first image is indexed.
    The runtime never downloads weights.  ``AGENT_KNOWLEDGE_VISION_MODEL_PATH``
    can point to a local CLIP directory; otherwise a sibling ``models`` folder
    under the knowledge data directory is checked.
    """

    def __init__(self, knowledge_root: Path):
        self.root = Path(knowledge_root)
        configured_path = os.getenv("AGENT_KNOWLEDGE_VISION_MODEL_PATH", "").strip()
        if configured_path:
            # An explicit path is authoritative.  Do not silently fall back to
            # a workstation cache when that path is missing or incomplete.
            candidates = [Path(configured_path).expanduser()]
        else:
            candidates = [self.root / "models" / "clip-vit-base-patch32"]
            # This is a local workstation model cache used by the school-cloud
            # runtime.  The resolved path is never returned by status().
            candidates.append(Path("/media/data3/dengkw/shared/models/clip-vit-base-patch32"))
        self.model_path = next((candidate for candidate in candidates if candidate.is_dir()), candidates[0])
        self.model_name = os.getenv("AGENT_KNOWLEDGE_VISION_MODEL", DEFAULT_CLIP_MODEL)
        configured_ocr_path = os.getenv("AGENT_KNOWLEDGE_OCR_MODEL_PATH", "").strip()
        self.ocr_model_path = Path(configured_ocr_path).expanduser() if configured_ocr_path else Path("")
        self._clip_model = None
        self._clip_processor = None
        self._clip_dimensions: int | None = None
        self._clip_source_id: str | None = None
        self._ocr = None
        self._lock = threading.RLock()
        # Torch modules and RapidOCR sessions are not guaranteed to be
        # re-entrant.  Loading and inference use separate locks so concurrent
        # requests can observe a stable state without running the same model
        # session at the same time.
        self._inference_lock = threading.RLock()
        self._clip_error: str | None = None
        self._ocr_error: str | None = None

    @staticmethod
    def _safe_error(exc: BaseException, fallback: str) -> str:
        """Return a stable diagnostic without paths, URLs, or secret values."""
        # Exception messages frequently contain local model paths or request
        # headers.  Status is an operator-facing capability endpoint, so only
        # the exception type and a fixed operation label are retained.
        return f"{type(exc).__name__}: {fallback}"

    def _record_clip_error(self, exc: BaseException) -> None:
        self._clip_error = self._safe_error(exc, "本地视觉模型不可用")

    def _record_ocr_error(self, exc: BaseException) -> None:
        self._ocr_error = self._safe_error(exc, "本地 OCR 不可用")

    def _clip_config(self) -> dict[str, Any] | None:
        config_path = self.model_path / "config.json"
        try:
            if not config_path.is_file() or config_path.stat().st_size > 2 * 1024 * 1024:
                return None
            with config_path.open("r", encoding="utf-8") as source:
                config = json.load(source)
            return config if isinstance(config, dict) else None
        except Exception as exc:
            self._record_clip_error(exc)
            return None

    def _model_source_id(self) -> str | None:
        """Return an opaque checkpoint identifier, never the local path."""
        if self._clip_source_id:
            return self._clip_source_id
        config_path = self.model_path / "config.json"
        try:
            digest = hashlib.sha256(config_path.read_bytes()).hexdigest()[:16]
        except Exception:
            return None
        self._clip_source_id = f"local-clip:{digest}"
        return self._clip_source_id

    def _configured_clip_dimensions(self) -> int | None:
        config = self._clip_config()
        try:
            value = int(config.get("projection_dim")) if config else 0
            return value if 1 <= value <= 4096 else None
        except (TypeError, ValueError):
            return None

    @property
    def clip_available(self) -> bool:
        """Whether a complete local checkpoint is present, without loading it."""
        if not self.model_path.is_dir() or self._configured_clip_dimensions() is None:
            return False
        config = self.model_path / "config.json"
        weights = any(
            (self.model_path / name).is_file() and (self.model_path / name).stat().st_size > 0
            for name in ("model.safetensors", "pytorch_model.bin")
        )
        processor_path = self.model_path / "preprocessor_config.json"
        processor = processor_path.is_file() and processor_path.stat().st_size > 0
        tokenizer = any(
            (self.model_path / name).is_file() and (self.model_path / name).stat().st_size > 0
            for name in ("tokenizer.json", "vocab.json", "tokenizer.model")
        )
        return config.is_file() and weights and processor and tokenizer

    @property
    def ocr_available(self) -> bool:
        try:
            import rapidocr_onnxruntime  # noqa: F401
            return True
        except Exception:
            return False

    def status(self) -> dict[str, Any]:
        """Return truthful capability metadata without loading model weights.

        ``ready`` means the runtime has loaded and successfully validated the
        session.  A present checkpoint or importable package is reported in the
        separate ``*_available`` fields and is never advertised as ready.
        """
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
            cuda_devices = int(torch.cuda.device_count()) if torch.cuda.is_available() else 0
        except Exception:
            device, cuda_devices = "cpu", 0
        configured_dimensions = self._configured_clip_dimensions()
        clip_loaded = self._clip_model is not None and self._clip_dimensions is not None
        ocr_loaded = self._ocr is not None
        return {
            "model": self.model_name,
            "model_path_configured": self.model_path.is_dir(),
            "model_path_exists": self.clip_available,
            "model_status": "loaded" if clip_loaded else ("configured" if self.clip_available else "missing"),
            "model_source": self._model_source_id() if self.clip_available else None,
            "visual_embedding": "ready" if clip_loaded else "needs_model",
            "visual_embedding_available": self.clip_available,
            "embedding_dimensions": self._clip_dimensions if clip_loaded else configured_dimensions,
            "ocr": "ready" if ocr_loaded else "needs_ocr",
            "ocr_available": self.ocr_available,
            "entity_extraction": "ocr+rules" if ocr_loaded else "needs_ocr",
            "relation_extraction": "co_occurrence_only",
            "device": device,
            "cuda_devices": cuda_devices,
            "local_only": True,
            "download_allowed": False,
            "errors": {"visual": self._clip_error, "ocr": self._ocr_error},
        }

    def _load_clip(self):
        with self._lock:
            if self._clip_model is not None:
                return self._clip_model, self._clip_processor
            if not self.clip_available:
                self._clip_error = "本地视觉模型目录不存在或缺少 config.json"
                return None, None
            try:
                import torch
                from transformers import CLIPModel, CLIPProcessor
                # ``local_files_only`` is a hard boundary: indexing never turns
                # into a network download because a checkpoint is missing.
                processor = CLIPProcessor.from_pretrained(str(self.model_path), local_files_only=True)
                model = CLIPModel.from_pretrained(str(self.model_path), local_files_only=True)
                device = "cuda" if torch.cuda.is_available() else "cpu"
                model = model.to(device)
                model.eval()
                dimensions = self._configured_clip_dimensions()
                if dimensions is None:
                    raise ValueError("invalid projection dimensions")
                self._clip_processor, self._clip_model = processor, model
                self._clip_dimensions = dimensions
                self._model_source_id()
                return model, processor
            except Exception as exc:
                self._record_clip_error(exc)
                return None, None

    def _check_image(self, path: Path) -> tuple[int, int] | None:
        """Validate image bytes and pixel bounds before model/OCR work."""
        try:
            candidate = Path(path)
            if not candidate.is_file() or candidate.is_symlink():
                raise ValueError("image file unavailable")
            if candidate.stat().st_size <= 0 or candidate.stat().st_size > MAX_IMAGE_BYTES:
                raise ValueError("image byte limit exceeded")
            from PIL import Image
            with Image.open(candidate) as image:
                width, height = image.size
                if width < 1 or height < 1 or width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
                    raise ValueError("image dimensions exceed limit")
                if width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("image pixel limit exceeded")
            return width, height
        except Exception as exc:
            # Do not include the rejected path in status diagnostics.
            self._record_clip_error(exc)
            self._record_ocr_error(exc)
            return None

    def _validated_vector(self, vector: Any, expected_dimensions: int | None) -> list[float] | None:
        try:
            values = vector.detach().float().cpu().reshape(-1).tolist()
            if not values or (expected_dimensions is not None and len(values) != expected_dimensions):
                return None
            if not all(math.isfinite(float(value)) for value in values):
                return None
            return _normalise([float(value) for value in values])
        except Exception:
            return None

    def embed_image(self, path: Path) -> dict[str, Any] | None:
        """Compute a normalized CLIP image vector, or return ``None`` safely."""
        if self._check_image(path) is None:
            return None
        model, processor = self._load_clip()
        if model is None:
            return None
        try:
            import torch
            from PIL import Image
            with self._inference_lock:
                with Image.open(path) as source:
                    image = source.convert("RGB")
                    inputs = processor(images=image, return_tensors="pt")
                device = next(model.parameters()).device
                inputs = {key: value.to(device) for key, value in inputs.items()}
                with torch.inference_mode():
                    # Transformers 4.57 returns a model output object from
                    # ``get_image_features``; use the explicit projection path so
                    # older and newer releases produce the same 512-D vector.
                    vision_output = model.vision_model(pixel_values=inputs["pixel_values"])
                    vector = model.visual_projection(vision_output.pooler_output)
                values = self._validated_vector(vector, self._clip_dimensions)
            if values is None:
                raise ValueError("visual embedding dimensions or values invalid")
            return {"model": self.model_name, "source": self._model_source_id(), "dimensions": len(values), "vector": values, "device": str(device)}
        except Exception as exc:
            self._record_clip_error(exc)
            return None

    def embed_text(self, text: str) -> dict[str, Any] | None:
        """Compute a CLIP text vector for bounded cross-modal retrieval."""
        text = " ".join(str(text or "").split())[:2000]
        if not text:
            return None
        model, processor = self._load_clip()
        if model is None:
            return None
        try:
            import torch
            with self._inference_lock:
                inputs = processor(text=[text], return_tensors="pt", padding=True, truncation=True)
                device = next(model.parameters()).device
                inputs = {key: value.to(device) for key, value in inputs.items()}
                with torch.inference_mode():
                    text_output = model.text_model(input_ids=inputs["input_ids"], attention_mask=inputs.get("attention_mask"))
                    pooled = text_output.pooler_output
                    vector = model.text_projection(pooled)
                values = self._validated_vector(vector, self._clip_dimensions)
            if values is None:
                raise ValueError("text embedding dimensions or values invalid")
            return {"model": self.model_name, "source": self._model_source_id(), "dimensions": len(values), "vector": values, "device": str(device)}
        except Exception as exc:
            self._record_clip_error(exc)
            return None

    def _load_ocr(self):
        with self._lock:
            if self._ocr is not None:
                return self._ocr
            try:
                from rapidocr_onnxruntime import RapidOCR
                # The package ships a pinned CPU-compatible detector/recognizer
                # pair.  A custom OCR directory is recorded for operators but
                # is not passed as version-specific kwargs to older RapidOCR.
                self._ocr = RapidOCR()
                return self._ocr
            except Exception as exc:
                self._record_ocr_error(exc)
                return None

    def ocr_image(self, path: Path) -> dict[str, Any] | None:
        """Return OCR text and boxes from the local RapidOCR runtime."""
        if self._check_image(path) is None:
            return None
        ocr = self._load_ocr()
        if ocr is None:
            return None
        try:
            import numpy as np
            from PIL import Image, ImageOps
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
                width, height = image.size
                pixels = np.asarray(image).copy()
            with self._inference_lock:
                result, _ = ocr(pixels)
            boxes, texts = [], []
            for item in result or []:
                if len(item) < 3 or float(item[2]) < 0.35:
                    continue
                polygon = [[int(point[0]), int(point[1])] for point in item[0]]
                text, confidence = str(item[1]), float(item[2])
                boxes.append({"polygon": polygon, "text": text, "confidence": round(confidence, 4)})
                texts.append(text)
            return {"text": "\n".join(texts), "boxes": boxes, "width": width, "height": height,
                    "model": "rapidocr-onnxruntime", "status": "ready"}
        except Exception as exc:
            self._record_ocr_error(exc)
            return None

    @staticmethod
    def region_for_box(box: dict[str, Any], regions: list[dict[str, Any]]) -> str | None:
        """Assign an OCR box to its containing grid region by box centroid."""
        polygon = box.get("polygon") or []
        if not polygon:
            return None
        cx = sum(point[0] for point in polygon) / len(polygon)
        cy = sum(point[1] for point in polygon) / len(polygon)
        for region in regions:
            if region["x"] <= cx < region["x"] + region["width"] and region["y"] <= cy < region["y"] + region["height"]:
                return region["id"]
        return None

    @staticmethod
    def asset_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()


def parse_json(value: str | None, default: Any):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default
