#!/usr/bin/env python3
"""Reflect Cloud Code session liveness into this project's task board.

The monitor only uses ``claude agents --json`` and the public ``boardctl``
commands. It never edits the shared board database directly and never marks a
task done. A worker that disappears before submitting is recorded as failed so
the project lead can decide whether to restart it; a reviewer disappearing is
recorded as an audit note while the task remains in review.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT = "open-claude-code"
BOARDCTL = Path("/media/data3/dengkw/projects/project-board/boardctl")
STATE_PATH = Path("/media/data3/dengkw/.runtime/open-claude-code/cloud-code-board-monitor.json")


@dataclass(frozen=True)
class Assignment:
    task_id: str
    st_id: str
    short_id: str
    session_name: str
    assignee: str
    role: str = "worker"


ASSIGNMENTS = (
    Assignment(
        task_id="OC-T3",
        st_id="ST-e8f7e353-b1d2-4400-916e-2b41bd44c76a",
        short_id="e8f7e353",
        session_name="orange-project-lead-finalize",
        assignee="橙色-项目子负责人",
    ),
    Assignment(
        task_id="OC-T1",
        st_id="ST-3b42bbae-6ad8-4be9-b97f-817d84ce2c31",
        short_id="3b42bbae",
        session_name="red-subtask-reviewer-finalize",
        assignee="红色-子任务审核人",
        role="reviewer",
    ),
    Assignment(
        task_id="OC-T4",
        st_id="ST-b62d4b1e-864f-405a-9c61-ba2d6a76f0b6",
        short_id="b62d4b1e",
        session_name="blue-project-lead",
        assignee="蓝色-项目子负责人",
    ),
    Assignment(
        task_id="OC-T2",
        st_id="ST-ebc167cb-e23c-4d2e-80b2-633fedf44e7c",
        short_id="ebc167cb",
        session_name="red-project-lead",
        assignee="红色-项目子负责人",
    ),
    Assignment(
        task_id="OC-T3",
        st_id="ST-5329b1d8-9e67-4ca7-b629-a6f8438fb8a2",
        short_id="5329b1d8",
        session_name="orange-subtask-reviewer",
        assignee="橙色-子任务审核人",
        role="reviewer",
    ),
    Assignment(
        task_id="OC-T8",
        st_id="ST-b8f8432f-12c0-4b0d-8986-e9edcd6ee85d",
        short_id="b8f8432f",
        session_name="purple-project-lead",
        assignee="紫色-项目子负责人",
    ),
    Assignment(
        task_id="OC-T11",
        st_id="ST-b52a6fc3-2e32-4791-b952-a556b22a4d4a",
        short_id="b52a6fc3",
        session_name="orange-plugin-finalize",
        assignee="橙色-项目子负责人",
    ),
    Assignment(
        task_id="OC-T11",
        st_id="ST-9343143d-1965-44b7-9eea-f60c38412643",
        short_id="9343143d",
        session_name="orange-plugin-reviewer",
        assignee="橙色-子任务审核人",
        role="reviewer",
    ),
    Assignment(
        task_id="OC-T2",
        st_id="ST-bf4df9c8-1069-42eb-a6e3-2d2bd114066e",
        short_id="bf4df9c8",
        session_name="red-subtask-reviewer-t2",
        assignee="红色-子任务审核人",
        role="reviewer",
    ),
    Assignment(
        task_id="OC-T4",
        st_id="ST-a8714322-7d96-42ad-b576-6554c7a12825",
        short_id="a8714322",
        session_name="blue-subtask-reviewer-t4",
        assignee="蓝色-子任务审核人",
        role="reviewer",
    ),
    Assignment(
        task_id="OC-T5",
        st_id="ST-7b1ed810-cfd2-4721-9cd4-58864e7e385f",
        short_id="7b1ed810",
        session_name="blue-project-lead-t5",
        assignee="蓝色-项目子负责人",
    ),
    Assignment(
        task_id="OC-T8",
        st_id="ST-9568a674-4641-4e4d-9ee9-d04bdce9cddd",
        short_id="9568a674",
        session_name="purple-subtask-reviewer-t8",
        assignee="紫色-子任务审核人",
        role="reviewer",
    ),
    Assignment(
        task_id="OC-T8",
        st_id="ST-2d356e9b-128c-4c0e-ae29-de1c975fbbdd",
        short_id="2d356e9b",
        session_name="purple-project-lead-rework",
        assignee="紫色-项目子负责人",
    ),
    Assignment(
        task_id="OC-T8",
        st_id="ST-cb41e9d7-a32e-4065-870f-742db754e96b",
        short_id="cb41e9d7",
        session_name="purple-subtask-reviewer-t8-rework",
        assignee="紫色-子任务审核人",
        role="reviewer",
    ),
    Assignment(
        task_id="OC-T9",
        st_id="ST-450fd31a-5863-4483-9bbf-fcea44190c58",
        short_id="450fd31a",
        session_name="green-project-lead-rework",
        assignee="绿色-项目子负责人",
    ),
    Assignment(
        task_id="OC-T9",
        st_id="ST-3a4f7fd0-52b3-4ac5-b6eb-141255602955",
        short_id="3a4f7fd0",
        session_name="green-subtask-reviewer-t9",
        assignee="绿色-子任务审核人",
        role="reviewer",
    ),
    Assignment(
        task_id="OC-T12",
        st_id="ST-5c8a2eb7-1a7a-40c8-8bf8-601847d632a6",
        short_id="5c8a2eb7",
        session_name="open-claude-code-ff",
        assignee="橙色-项目子负责人 Claude Code",
    ),
)


def _run(command: list[str], *, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)


def read_sessions(raw: str) -> dict[str, dict[str, Any]]:
    """Index the Claude Code registry by full and short session identifiers."""
    values = json.loads(raw)
    if not isinstance(values, list):
        raise ValueError("claude agents output must be a list")
    result: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, dict):
            continue
        full = value.get("sessionId")
        short = value.get("id")
        if isinstance(full, str) and full:
            result[full] = value
        if isinstance(short, str) and short:
            result[short] = value
    return result


def session_state(session: dict[str, Any] | None) -> str:
    if not session:
        return "missing"
    status = session.get("status")
    if session.get("state") in {"done", "completed", "stopped"}:
        return "done"
    if status == "busy" or session.get("state") in {"working", "running"}:
        return "running"
    if status == "idle":
        return "idle"
    return str(status or "unknown")


def planned_actions(assignment: Assignment, session: dict[str, Any] | None, task_status: str, previous: str | None) -> list[dict[str, Any]]:
    """Return safe board actions for one session transition.

    ``done`` is deliberately absent. The executor or reviewer must submit and
    independently review a node before the board can advance it.
    """
    current = session_state(session)
    actions: list[dict[str, Any]] = []
    if current == "running":
        if previous != current:
            actions.append({
                "kind": "note",
                "task": assignment.task_id,
                "text": (
                    f"Cloud Code 会话状态：ST={assignment.st_id}，会话={assignment.session_name}，"
                    f"session_status=running，角色={assignment.role}。已收到真实运行回执。"
                ),
            })
        return actions
    if assignment.role == "reviewer":
        if current == "done" and previous != "done" and task_status != "done":
            actions.append({
                "kind": "note",
                "task": assignment.task_id,
                "text": (
                    f"独立审核 Cloud Code 会话已结束或失联：ST={assignment.st_id}，"
                    f"session_status={current}。任务仍保持 {task_status}，等待审核结果；未自动判定通过。"
                ),
            })
        elif previous == "running" and current in {"idle", "missing", "unknown"}:
            actions.append({
                "kind": "note",
                "task": assignment.task_id,
                "text": f"独立审核 Cloud Code 会话暂时失联：ST={assignment.st_id}，session_status={current}；任务仍保持 {task_status}。",
            })
        return actions
    if current == "done" and task_status == "running":
        actions.append({
            "kind": "note",
            "task": assignment.task_id,
            "text": (
                f"Cloud Code 执行会话已完成但未提交审核：ST={assignment.st_id}，"
                "节点不能继续显示进行中。"
            ),
        })
        actions.append({"kind": "fail", "task": assignment.task_id})
    elif current in {"idle", "missing", "unknown"} and task_status == "running":
        actions.append({
            "kind": "note",
            "task": assignment.task_id,
            "text": (
                f"Cloud Code 执行会话已结束或失联：ST={assignment.st_id}，"
                f"session_status={current}。会话未提交审核，任务不能继续显示进行中。"
            ),
        })
        actions.append({"kind": "fail", "task": assignment.task_id})
    return actions


def board_status() -> dict[str, dict[str, Any]]:
    result = _run([str(BOARDCTL), "status", "--project", PROJECT])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "boardctl status failed")
    payload = json.loads(result.stdout)
    return {task["id"]: task for task in payload.get("tasks", [])}


def apply_action(action: dict[str, Any]) -> None:
    task = action["task"]
    if action["kind"] == "note":
        command = [str(BOARDCTL), "note", "--task", task, "--actor", "pm", "--kind", "progress", "--text", action["text"]]
    elif action["kind"] == "fail":
        command = [str(BOARDCTL), "task", "update", "--task", task, "--status", "failed", "--actor", "pm"]
    else:
        raise ValueError(f"unsupported action: {action['kind']}")
    result = _run(command)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"boardctl action failed: {action['kind']}")


def load_state() -> dict[str, str]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, STATE_PATH)


def once() -> int:
    registry = _run(["claude", "agents", "--json"])
    if registry.returncode != 0:
        raise RuntimeError(registry.stderr.strip() or "claude agents failed")
    sessions = read_sessions(registry.stdout)
    tasks = board_status()
    state = load_state()
    next_state = dict(state)
    for assignment in ASSIGNMENTS:
        session = sessions.get(assignment.st_id.removeprefix("ST-")) or sessions.get(assignment.short_id)
        task = tasks.get(assignment.task_id)
        if not task:
            continue
        previous = state.get(assignment.st_id)
        current = session_state(session)
        for action in planned_actions(assignment, session, task["status"], previous):
            apply_action(action)
        next_state[assignment.st_id] = current
    save_state(next_state)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument("--interval", type=float, default=5.0, help="poll interval in seconds")
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval must be at least 1 second")
    if args.once:
        once()
        return 0
    while True:
        try:
            once()
        except Exception as exc:  # keep the monitor alive; next poll may recover
            print(f"cloud-code-board-monitor: {exc}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
