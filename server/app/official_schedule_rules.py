"""Reuse the official Harness Cron / RFC5545 calendar implementation.

Separate bounded worker with no inherited credentials, no customer prompts
and no execution capability exposed to the scheduling model. The existing
SQLite queue remains authoritative, not a second upstream Host scheduler.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

from harness.profile import PINNED_COMMIT
from .security import AppError

MAX_RULE_CHARS = 2048
# Keep user-driven validations from consuming the scheduler's capacity.
_CREATE_SLOT = threading.BoundedSemaphore(1)


def _utc(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
        return stamp.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise AppError(503, "SCHEDULE_RULE_RECEIPT_INVALID", "官方调度模块返回的时间无效") from None


def _call(settings, request):
    if not settings.harness_source_dir or not settings.harness_node_path:
        raise AppError(503, "SCHEDULE_RULE_ENGINE_UNAVAILABLE", "高级周期任务需要已安装的官方 Harness 调度模块")
    source = Path(settings.harness_source_dir).resolve()
    node = Path(settings.harness_node_path).resolve()
    script = Path(__file__).resolve().parents[1] / "harness/schedule_rules.mts"
    loader = source / "node_modules/tsx/dist/esm/index.mjs"
    if not node.is_file() or not loader.is_file() or not script.is_file():
        raise AppError(503, "SCHEDULE_RULE_ENGINE_UNAVAILABLE", "官方调度模块尚未就绪")
    root = settings.data_dir / "schedule-rule-runtime"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # No inherited model, proxy or credential variables.
    env = {"PATH":f"{node.parent}:/usr/bin:/bin", "HOME":str(root), "TMPDIR":str(root),
           "XDG_CONFIG_HOME":str(root), "XDG_CACHE_HOME":str(root),
           "TSX_TSCONFIG_PATH":str(source / "tsconfig.json"), "LANG":"C.UTF-8"}
    try:
        actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"],
            env=env, stderr=subprocess.DEVNULL, text=True, timeout=5).strip()
        if actual != PINNED_COMMIT:
            raise ValueError("unexpected source")
        subprocess.run(["git","-C",str(source),"diff","--quiet","HEAD","--"],
                       env=env, stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=5)
        result = subprocess.run([str(node),"--import",str(loader),str(script),str(source)],
            cwd=root, env=env,input=json.dumps(request),text=True,capture_output=True,timeout=10,check=True)
        if len(result.stdout)>8192:
            raise ValueError("oversized receipt")
        value=json.loads(result.stdout)
        if type(value) is not dict or type(value.get("ok")) is not bool:
            raise ValueError("invalid receipt")
    except (OSError,ValueError,subprocess.SubprocessError):
        raise AppError(503,"SCHEDULE_RULE_ENGINE_UNAVAILABLE","官方调度模块执行或版本校验失败") from None
    if not value["ok"]:
        codes={"invalid_time_zone":"时区无效","frequency_too_high":"周期频率过高",
               "not_future":"规则没有未来执行时间","time_out_of_range":"规则时间超出支持范围"}
        code=value.get("code")
        if not isinstance(code,str):
            code="invalid_rule"
        raise AppError(422,"SCHEDULE_RULE_INVALID",codes.get(code,"高级周期规则无效"))
    return value


def create_rule(settings, kind, expression, zone, reference):
    if (kind not in ("cron","rrule") or not isinstance(expression,str)
            or not expression.strip() or len(expression)>MAX_RULE_CHARS):
        raise AppError(422,"SCHEDULE_RULE_INVALID","请填写有效的 Cron 或 RRULE 规则")
    reference=reference.astimezone(timezone.utc)
    if not _CREATE_SLOT.acquire(blocking=False):
        raise AppError(429,"SCHEDULE_RULE_BUSY","正在验证其他周期规则，请稍后重试")
    try:
        result=_call(settings,{"operation":"create","kind":kind,"expression":expression,"timezone":zone,"now":reference.isoformat()})
    finally:
        _CREATE_SLOT.release()
    next_at=_utc(result.get("next"))
    normalized=result.get("expression")
    if not isinstance(normalized,str) or not normalized or len(normalized)>MAX_RULE_CHARS:
        raise AppError(503,"SCHEDULE_RULE_RECEIPT_INVALID","官方调度模块返回的规则无效")
    if not reference<next_at<=reference+timedelta(days=365):
        raise AppError(422,"SCHEDULE_INVALID","首次执行时间须在未来一年内")
    normalized_zone=result.get("timezone") if kind=="cron" else zone
    if not isinstance(normalized_zone,str) or not normalized_zone:
        raise AppError(503,"SCHEDULE_RULE_RECEIPT_INVALID","官方调度模块返回的时区无效")
    return next_at.isoformat(),normalized,normalized_zone


def resolve_rule(settings, kind, expression, zone, next_at, reference):
    due=_utc(next_at)
    reference=reference.astimezone(timezone.utc)
    if reference<due:
        raise AppError(409,"SCHEDULE_NOT_DUE","任务尚未到期")
    result=_call(settings,{"operation":"resolve","kind":kind,"expression":expression,
                           "timezone":zone,"scheduled_at":due.isoformat(timespec="milliseconds").replace("+00:00","Z"),"now":reference.isoformat()})
    occurrence=_utc(result.get("occurrence"))
    following=_utc(result["next"]) if result.get("next") is not None else None
    if not due<=occurrence<=reference or following is not None and following<=reference:
        raise AppError(503,"SCHEDULE_RULE_RECEIPT_INVALID","官方调度模块返回的周期顺序无效")
    return following.isoformat() if following else None, occurrence.isoformat()


def resolve_resume_rule(settings, kind, expression, zone, next_at, reference):
    """User-triggered recalculation shares the bounded creation slot."""
    if not _CREATE_SLOT.acquire(blocking=False):
        raise AppError(429,"SCHEDULE_RULE_BUSY","正在验证其他周期规则，请稍后重试")
    try:
        return resolve_rule(settings,kind,expression,zone,next_at,reference)
    finally:
        _CREATE_SLOT.release()
