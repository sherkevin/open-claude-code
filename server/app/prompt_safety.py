"""Admission for user text. The deployment policy and Qwen use separate labels."""
from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from pathlib import Path
from typing import Literal, get_args
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .security import AppError

log = logging.getLogger(__name__)

QWEN_MAX_CHARS = 8000
MAX_POLICY_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 32 * 1024
BLOCKED = "PROMPT_BLOCKED"
UNAVAILABLE = "PROMPT_REVIEW_UNAVAILABLE"
DEFAULT_BLOCK_MESSAGE = "这条消息因内容安全策略未发送，请调整后重试。"
UNAVAILABLE_MESSAGE = "内容审核暂时不可用，消息未发送，请稍后重试。"

QwenCategory = Literal[
    "Violent", "Non-violent Illegal Acts", "Sexual Content or Sexual Acts", "PII",
    "Suicide & Self-Harm", "Unethical Acts", "Copyright Violation", "Jailbreak",
]
GENERAL_CATEGORIES = frozenset(get_args(QwenCategory))
NATIVE_CATEGORIES = GENERAL_CATEGORIES | {"Politically Sensitive Topics"}
REVIEW_LABEL = re.compile(r"Safety: (Safe|Unsafe|Controversial)\r?\nCategories: ([^\r\n]+)")
FRAMING = re.compile(r"<\|[a-z_]+\|>|</?think>|<(?:BEGIN|END) (?:CONVERSATION|SAFETY POLICY|UNSAFE CONTENT CATEGORIES)>", re.I)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in text if not ch.isspace() and not unicodedata.category(ch).startswith(("Cf", "P")))


class TopicRule(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    category: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,59}$")
    enabled: bool = True
    terms: list[str] = Field(min_length=1, max_length=64)
    require_any: list[str] = Field(default_factory=list, max_length=64)
    except_exact: list[str] = Field(default_factory=list, max_length=64)
    message: str | None = Field(default=None, min_length=1, max_length=160)

    @field_validator("terms", "require_any", "except_exact")
    @classmethod
    def normalize_terms(cls, values: list[str]) -> list[str]:
        normalized = [normalize(value) for value in values]
        if any(len(value) > 400 for value in values) or any(not value for value in normalized):
            raise ValueError("invalid policy term")
        return normalized

    def matches(self, normalized: str) -> bool:
        return (self.enabled and normalized not in self.except_exact
                and any(term in normalized for term in self.terms)
                and (not self.require_any or any(term in normalized for term in self.require_any)))


class PromptPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rules: list[TopicRule] = Field(max_length=100)
    blocked_message: str = Field(default=DEFAULT_BLOCK_MESSAGE, min_length=1, max_length=160)
    qwen_block_levels: list[Literal["Unsafe", "Controversial"]] = Field(default_factory=lambda: ["Unsafe"], min_length=1, max_length=2)
    qwen_block_categories: list[QwenCategory] = Field(default_factory=lambda: sorted(GENERAL_CATEGORIES), min_length=1, max_length=8)

    def blocked_qwen_categories(self, level: str, categories: frozenset[str]) -> frozenset[str]:
        return categories.intersection(self.qwen_block_categories) if level in self.qwen_block_levels else frozenset()


class ReviewFailure(Exception):
    """A fixed operational category safe to log."""


def _endpoint(base_url: str) -> str:
    try:
        url = urlsplit(base_url)
        if (url.scheme in {"http", "https"} and url.hostname in {"127.0.0.1", "::1"}
                and url.username is None and url.password is None and url.port
                and url.path in {"/v1", "/v1/"} and not (url.query or url.fragment)):
            host = "[::1]" if url.hostname == "::1" else "127.0.0.1"
            return f"{url.scheme}://{host}:{url.port}/v1/chat/completions"
    except ValueError:
        pass
    raise ReviewFailure("configuration")


def _parse_qwen_response(raw: bytes) -> tuple[str, frozenset[str]]:
    try:
        choices = json.loads(raw)["choices"]
        if not isinstance(choices, list) or len(choices) != 1 or choices[0]["finish_reason"] != "stop":
            raise ReviewFailure("response")
        content = choices[0]["message"]["content"]
    except (KeyError, TypeError, ValueError, RecursionError):
        raise ReviewFailure("response") from None
    return parse_qwen_decision(content)


def parse_qwen_decision(content: str) -> tuple[str, frozenset[str]]:
    match = REVIEW_LABEL.fullmatch(content.strip()) if isinstance(content, str) else None
    if not match:
        raise ReviewFailure("response")
    level, labels = match.groups()
    categories = frozenset(part.strip() for part in labels.split(","))
    if level == "Safe" and categories == {"None"}:
        return level, frozenset()
    if level == "Safe" or not categories <= NATIVE_CATEGORIES:
        raise ReviewFailure("response")
    return level, categories


class PromptSafety:
    def __init__(self, settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return self.settings.prompt_safety_mode != "off"

    def _read_policy(self) -> PromptPolicy:
        try:
            with Path(self.settings.prompt_safety_policy).open("rb") as file:
                data = file.read(MAX_POLICY_BYTES + 1)
            if len(data) > MAX_POLICY_BYTES:
                raise ReviewFailure("policy")
            return PromptPolicy.model_validate_json(data)
        except (OSError, ValueError):
            raise ReviewFailure("policy") from None

    def classify(self, text: str) -> tuple[str, frozenset[str]]:
        """Use the classifier's native chat template and two-line response."""
        endpoint = _endpoint(self.settings.prompt_safety_qwen_url)
        model = self.settings.prompt_safety_qwen_model
        try:
            timeout = float(self.settings.prompt_safety_timeout_seconds)
            if not model or not 0 < timeout <= 120:
                raise ValueError
        except ValueError:
            raise ReviewFailure("configuration") from None
        deadline = time.monotonic() + timeout
        payload = {"model": model, "messages": [{"role": "user", "content": text}],
                   "temperature": 0, "max_tokens": 128, "stream": False}
        try:
            with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False,
                              headers={"Accept-Encoding": "identity"}) as client:
                with client.stream("POST", endpoint, json=payload) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() >= deadline:
                            raise ReviewFailure("timeout")
                        if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ReviewFailure("response")
                        raw.extend(chunk)
        except httpx.TimeoutException:
            raise ReviewFailure("timeout") from None
        except httpx.HTTPStatusError:
            raise ReviewFailure("http_status") from None
        except (httpx.HTTPError, UnicodeError):
            raise ReviewFailure("transport") from None
        return _parse_qwen_response(raw)

    def require_allowed(self, text: str) -> None:
        if not self.enabled or not text.strip():
            return
        try:
            mode = self.settings.prompt_safety_mode
            if mode not in {"rules", "qwen"}:
                raise ReviewFailure("configuration")
            policy = self._read_policy()
            if policy.rules:
                normalized = normalize(text)
                for rule in policy.rules:
                    if rule.matches(normalized):
                        log.info("Prompt safety blocked source=policy category=%s", rule.category)
                        raise AppError(403, BLOCKED, rule.message or policy.blocked_message)
            if mode == "qwen":
                if len(text) > QWEN_MAX_CHARS:
                    raise AppError(422, "PROMPT_REVIEW_LIMIT", "消息超过内容审核长度上限，请拆分后发送。")
                if FRAMING.search(text):
                    log.info("Prompt safety blocked source=classifier category=reserved_format")
                    raise AppError(403, BLOCKED, policy.blocked_message)
                level, categories = self.classify(text)
                blocked = policy.blocked_qwen_categories(level, categories)
                if blocked:
                    log.info("Prompt safety blocked source=classifier category=%s", ",".join(sorted(blocked)))
                    raise AppError(403, BLOCKED, policy.blocked_message)
        except ReviewFailure as exc:
            log.warning("Prompt safety unavailable category=%s", exc)
            raise AppError(503, UNAVAILABLE, UNAVAILABLE_MESSAGE) from None
