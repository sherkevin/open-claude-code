"""Server-owned public model names and private, pinned execution selections."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .security import AppError


class ModelCatalog:
    def __init__(self, settings):
        self.settings = settings
        if settings.model_catalog:
            document = json.loads(Path(settings.model_catalog).read_text(encoding="utf-8"))
        else:
            # Compatibility for existing single-model installations. Never use a
            # provider's private deployment identifier as the public product name.
            document = {"default_model_id": "legacy-model", "models": [{
                "id": "legacy-model", "label": "默认模型", "upstream_model": settings.model,
                "reasoning_levels": {settings.effort: settings.effort}, "default_reasoning_level": settings.effort,
            }]}
        self.models = {}
        for value in document.get("models", []):
            item = dict(value)
            identity, label = item.get("id"), item.get("label")
            levels = item.get("reasoning_levels", {"default": None})
            default = item.get("default_reasoning_level", next(iter(levels), None))
            if (not isinstance(identity, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,99}", identity)
                    or identity in self.models or not isinstance(label, str) or not label or len(label) > 100
                    or not isinstance(levels, dict) or not levels or default not in levels
                    or any(not isinstance(k, str) or not k or len(k) > 30 or (v is not None and v not in {"low", "medium", "high", "max"}) for k, v in levels.items())
                    or not isinstance(item.get("upstream_model", ""), str)
                    or type(item.get("requires_new_conversation_on_switch", False)) is not bool):
                raise ValueError("Invalid model catalog entry")
            overrides = item.get("provider_env", {})
            if not isinstance(overrides, dict) or any(k not in {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"} or not isinstance(v, str) for k, v in overrides.items()):
                raise ValueError("Invalid model provider overrides")
            capabilities = item.get("capabilities", [])
            if (not isinstance(capabilities, list) or len(capabilities) > 20
                    or any(not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,39}", value) for value in capabilities)):
                raise ValueError("Invalid model capabilities")
            unavailable_reason = item.get("unavailable_reason")
            if unavailable_reason is not None and (not isinstance(unavailable_reason, str) or not unavailable_reason or len(unavailable_reason) > 200):
                raise ValueError("Invalid model unavailable reason")
            item["capabilities"] = list(dict.fromkeys(capabilities))
            item.update(reasoning_levels=levels, default_reasoning_level=default)
            self.models[identity] = item
        self.default_model_id = settings.default_model_id or document.get("default_model_id")
        if self.default_model_id not in self.models:
            raise ValueError("Model catalog must specify an existing default_model_id")

    def public(self):
        result = []
        for item in self.models.values():
            available = bool(item.get("enabled", True) and item.get("upstream_model"))
            entry = {"id": item["id"], "label": item["label"], "available": available,
                     "requires_new_conversation_on_switch": item.get("requires_new_conversation_on_switch", False),
                     "capabilities": list(item.get("capabilities", [])),
                     "reasoning_levels": [{"id": level, "label": "默认" if level == "default" else level} for level in item["reasoning_levels"]],
                     "default_reasoning_level": item["default_reasoning_level"]}
            if not available:
                entry["unavailable_reason"] = item.get("unavailable_reason") or "此模型尚未开通，请联系管理员"
            result.append(entry)
        return result

    @property
    def defaults(self):
        item = self.models[self.default_model_id]
        return {"default_model_id": item["id"], "default_reasoning_level": item["default_reasoning_level"], "model_label": item["label"]}

    def select(self, model_id=None, reasoning_level=None):
        item = self.models.get(model_id or self.default_model_id)
        if item is None:
            raise AppError(422, "UNKNOWN_MODEL", "请选择模型列表中的模型")
        if not item.get("enabled", True) or not item.get("upstream_model"):
            raise AppError(503, "MODEL_UNAVAILABLE", "此模型尚未开通，请选择其他模型或联系管理员")
        level = reasoning_level or item["default_reasoning_level"]
        if level not in item["reasoning_levels"]:
            raise AppError(422, "UNSUPPORTED_REASONING_LEVEL", "当前模型不支持此推理级别")
        return {"model_id": item["id"], "model_label": item["label"], "reasoning_level": level,
                "upstream_model": item["upstream_model"], "upstream_effort": item["reasoning_levels"][level]}

    def validate_snapshot(self, selection):
        """Fail explicitly when an administrator changes a pinned route."""
        if selection.get("legacy"):
            if selection["upstream_model"] != self.settings.model:
                raise AppError(409, "MODEL_SELECTION_REQUIRED", "历史模型配置已变化，请明确选择模型后继续")
            return selection
        current = self.select(selection["model_id"], selection["reasoning_level"])
        if any(current[key] != selection[key] for key in ("upstream_model", "upstream_effort")):
            raise AppError(409, "MODEL_SELECTION_REQUIRED", "模型服务配置已变化，请重新选择模型后继续")
        return selection

    def historical(self, upstream_model):
        for item in self.models.values():
            if item.get("upstream_model") == upstream_model:
                # Preserve the actual pre-migration effort as well as its route.
                for level, effort in item["reasoning_levels"].items():
                    if effort == self.settings.effort:
                        return {"model_id": item["id"], "model_label": item["label"], "reasoning_level": level,
                                "upstream_model": upstream_model, "upstream_effort": effort}
        return {"model_id": "legacy-model", "model_label": "历史模型", "reasoning_level": self.settings.effort,
                "upstream_model": upstream_model, "upstream_effort": self.settings.effort, "legacy": True}

    def provider_overrides(self, selection):
        item = self.models.get(selection["model_id"], {})
        return dict(item.get("provider_env", {}))

    def requires_new_conversation(self, selection):
        item = self.models.get(selection.get("model_id"), {})
        if item.get("requires_new_conversation_on_switch", False):
            return True
        # A historical/renamed public alias cannot bypass a restriction on its
        # actual upstream route. Do not expose that private route to the client.
        return any(entry.get("requires_new_conversation_on_switch", False)
                   and entry.get("upstream_model") == selection.get("upstream_model")
                   for entry in self.models.values())
