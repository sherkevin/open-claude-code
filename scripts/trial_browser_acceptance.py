#!/usr/bin/env python3
"""Run real Chromium acceptance against an isolated synthetic trial server.

The script starts the product API on loopback, creates synthetic assistants,
knowledge and image fixtures, then asks the companion Playwright script to
serve the production client build through Vite preview.  It never touches the
shared product service, public ports, customer data or a paid model.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from trial_smoke import (  # noqa: E402
    PASSWORD,
    SyntheticRuntime,
    SyntheticTrialServer,
    require,
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def synthetic_png(width: int = 96, height: int = 64) -> bytes:
    """Build a deterministic PNG without Pillow or an external asset."""
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row.extend(
                (
                    42 + (x * 2) % 120,
                    80 + (y * 3) % 100,
                    96 + ((x + y) * 2) % 90,
                )
            )
        rows.append(bytes(row))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + _png_chunk(b"IEND", b"")
    )


class BrowserRuntime(SyntheticRuntime):
    """Synthetic runtime with real knowledge, approval and artifact paths."""

    stream_delay = 0.12

    async def execute(self, run, message) -> None:
        cls = type(self)
        cls.executions += 1
        text = str(message.get("text") or "")
        if "浏览器取消" in text:
            await self.emit(
                "message.delta",
                {
                    "message_id": run["assistant_message_id"],
                    "text": "开始一个可取消的合成流。",
                },
            )
            await self.stopped.wait()
            raise asyncio.CancelledError()

        if "浏览器知识证据" in text:
            user = self.service.require("users", run["user_id"])
            conversation = self.service.require("conversations", run["conversation_id"])
            assistant = self.service.usable_assistant(conversation["assistant_id"], user)
            result = await self.service.knowledge.search_for_run(
                user,
                assistant,
                run["id"],
                "P-101 维护周期",
                limit=4,
            )
            require(result["results"], "browser knowledge search returned no synthetic hit")
            citation = result["results"][0]["citation"]
            reply = (
                "知识检索完成：泵站阀门 P-101 的维护周期为每周一次。"
                f"{citation}"
            )
        elif "浏览器审批" in text:
            await self.emit(
                "tool.started",
                {
                    "tool_name": "mcp__workspace__file_write",
                    "arguments": {"path": "browser-approval.txt", "content": "approved"},
                },
            )
            allowed = await self.approve(
                "mcp__workspace__file_write",
                {"path": "browser-approval.txt", "content": "approved"},
            )
            await self.emit(
                "tool.completed",
                {
                    "tool_name": "mcp__workspace__file_write",
                    "success": bool(allowed),
                },
            )
            reply = "审批完成：合成写入已允许。" if allowed else "审批完成：合成写入未获允许。"
        elif "浏览器Excel" in text:
            from app.artifacts import write_xlsx

            workspace = self.service.settings.data_dir / "workspaces" / run["conversation_id"]
            path, size = write_xlsx(
                workspace,
                "browser-acceptance.xlsx",
                [
                    {
                        "name": "合成验收",
                        "columns": ["阀门", "周期"],
                        "rows": [["P-101", "每周一次"]],
                    }
                ],
            )
            reply = f"Excel 产物已生成：{path.name}（{size} 字节）。"
        else:
            reply = "浏览器合成回复：" + text

        for start in range(0, len(reply), 8):
            if self.stopped.is_set():
                raise asyncio.CancelledError()
            await self.emit(
                "message.delta",
                {
                    "message_id": run["assistant_message_id"],
                    "text": reply[start:start + 8],
                },
            )
            await asyncio.sleep(0.5 if "浏览器流式" in reply else cls.stream_delay)
        await self.emit(
            "usage.updated",
            {
                "input_tokens": 8,
                "output_tokens": max(1, len(reply) // 4),
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "estimated_cost_usd": 0,
                "currency": "USD",
                "cost_is_estimate": False,
                "source": "synthetic_browser_runtime",
                "model_call_count": 0,
            },
        )


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def create_fixture(server: SyntheticTrialServer) -> dict[str, str]:
    server.create_admin()
    service = server.app.state.service
    user = service.require(
        "users",
        service.db.one("SELECT id FROM users WHERE username=?", ("synthetic-admin",))["id"],
    )
    kb = service.knowledge.save(
        {
            "name": "华微浏览器合成资料",
            "description": "学校云真实 Chromium 验收专用合成资料",
            "scope": "private",
            "enabled": True,
            "default_for_new_assistants": False,
        },
        user,
    )
    document = service.knowledge.upload(
        kb["id"],
        user,
        "synthetic-browser-manual.md",
        (
            "# 合成浏览器手册\n\n"
            "泵站阀门编号 P-101，维护周期为每周一次。\n"
            "本文件只用于学校云真实浏览器验收，不包含客户资料。\n"
        ).encode("utf-8"),
    )
    image = service.knowledge.upload(
        kb["id"],
        user,
        "synthetic-browser-image.png",
        synthetic_png(),
    )
    assistant = service.save_assistant(
        {
            "name": "华微浏览器合成助手",
            "description": "真实 Chromium 验收，只使用合成资料。",
            "instructions": "只使用合成资料回答，不访问外部服务。",
            "knowledge_base_ids": [kb["id"]],
            "connections": [],
            "skill_ids": [],
            "web_search_enabled": False,
        },
        user=user,
    )
    return {
        "user_id": user["id"],
        "kb_id": kb["id"],
        "document_id": document["id"],
        "image_id": image["id"],
        "assistant_id": assistant["id"],
    }


def run_node_browser(
    *,
    mode: str,
    base_url: str,
    preview_url: str,
    output_dir: Path,
    report_path: Path,
) -> dict[str, Any]:
    # Chromium's singleton socket has a strict path-length limit.  Keep this
    # temporary directory on the same data disk while using a short prefix.
    browser_tmp = Path("/media/data3/dengkw/.oc-t13-chrome-tmp")
    browser_tmp.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "BROWSER_MODE": mode,
        "BROWSER_BASE_URL": base_url,
        "BROWSER_PREVIEW_URL": preview_url,
        "BROWSER_OUTPUT_DIR": str(output_dir),
        "BROWSER_REPORT_PATH": str(report_path),
        "BROWSER_USERNAME": "synthetic-admin",
        "BROWSER_PASSWORD": PASSWORD,
        "BROWSER_PREVIEW_PORT": preview_url.rsplit(":", 1)[-1],
        "VITE_MODULE_ENTRY": str(ROOT / "client/node_modules/vite/dist/node/index.js"),
        "PLAYWRIGHT_MODULE_ENTRY": str(
            ROOT
            / ".runtime/third_party/deepseek-harness/node_modules/.pnpm"
            / "playwright@1.63.0-alpha-2026-08-31/node_modules/playwright/index.mjs"
        ),
        "PLAYWRIGHT_CHROMIUM": "/home/dengkw/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome",
        "PLAYWRIGHT_BROWSERS_PATH": "/home/dengkw/.cache/ms-playwright",
        "TMPDIR": str(browser_tmp),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["node", str(ROOT / "scripts/browser_acceptance.mjs")],
        cwd=ROOT / "client",
        env=env,
        text=True,
        capture_output=True,
        timeout=360,
    )
    (report_path.with_suffix(".stdout.log")).write_text(result.stdout, encoding="utf-8")
    (report_path.with_suffix(".stderr.log")).write_text(result.stderr, encoding="utf-8")
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    else:
        report = {
            "status": "failed",
            "error": "browser report was not produced",
            "exit_code": result.returncode,
            "stdout_tail": result.stdout[-2000:],
            "stderr_tail": result.stderr[-4000:],
        }
    report["node_exit_code"] = result.returncode
    if result.returncode != 0 and report.get("status") == "passed":
        report["status"] = "failed"
        report.setdefault("issues", []).append("browser process exited nonzero")
    return report


async def _run_mode(mode: str, root: Path) -> dict[str, Any]:
    preview_port = free_loopback_port()
    preview_url = f"http://127.0.0.1:{preview_port}"
    data_dir = root / f"data-{mode}-{int(time.time() * 1000)}"
    server = await SyntheticTrialServer.start(
        data_dir,
        max_active_sessions=4,
        idle_seconds=30,
        runtime_factory=BrowserRuntime,
        sqlite_synchronous="FULL",
        deployment_mode=mode,
        origins=(preview_url,),
        approval_seconds=120,
    )
    try:
        fixture = create_fixture(server)
        report_path = root / f"browser-{mode}.json"
        report = await asyncio.to_thread(
            run_node_browser,
            mode=mode,
            base_url=server.base_url,
            preview_url=preview_url,
            output_dir=root / f"screenshots-{mode}",
            report_path=report_path,
        )
        report["fixture"] = fixture
        report["server_base_url"] = server.base_url
        report["preview_url"] = preview_url
        report["synthetic"] = True
        report["runtime"] = {
            "executions": BrowserRuntime.executions,
            "model_calls": BrowserRuntime.model_calls,
        }
        report["generated_at"] = now()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    finally:
        await server.stop()


async def run_acceptance(root: Path) -> dict[str, Any]:
    root = root.resolve()
    report: dict[str, Any] = {
        "schema_version": "trial-browser-acceptance/v1",
        "generated_at": now(),
        "synthetic": True,
        "mode": "isolated-loopback-production-build",
        "limitations": [
            "真实 Chromium 只访问本次临时回环服务，不访问共享产品或公网。",
            "模型、知识检索与审批使用注入的确定性合成运行时，model_calls=0。",
            "trial 模式不开放审批、Excel、上传和管理写入；这些路径在独立 normal 合成实例验证，不能解释为 trial 已开放。",
            "真实客户数据、真实模型、跨网、多进程和权限矩阵未验收。",
        ],
        "modes": {},
    }
    try:
        for mode in ("trial", "normal"):
            report["modes"][mode] = await _run_mode(mode, root)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    failures = [
        mode
        for mode, value in report["modes"].items()
        if value.get("status") != "passed"
    ]
    report["status"] = "passed" if not report.get("error") and not failures else "failed"
    report["summary"] = {
        "status": report["status"],
        "modes": len(report["modes"]),
        "failed_modes": failures,
        "model_calls": sum(
            int((value.get("runtime") or {}).get("model_calls") or 0)
            for value in report["modes"].values()
        ),
    }
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run real Chromium trial acceptance on school cloud.")
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "docs/evidence/TRIAL_BROWSER_ACCEPTANCE_2026-11-13.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / ".runtime/evidence/oc_t13_browser_20261113",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.output_dir = args.output_dir.resolve()
    args.report = args.report.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(run_acceptance(args.output_dir))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "summary": report["summary"],
                "error": report.get("error"),
                "report": str(args.report),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
