"""Reproducible HTTP acceptance checks; model calls require --real-model.

Credentials are read from environment or an owner-controlled JSON file. This
script never prints passwords, tokens, full request bodies or model responses.
It creates clearly labeled synthetic resources and disables them at the end
unless --keep-resources is set. It does not touch production business data.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
import time
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

import httpx


TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
REQUIRED_TOOLS = {"search_knowledge", "create_ticket", "list_tickets", "slow_lookup"}


class CheckFailure(RuntimeError):
    """A deliberately safe assertion message, without raw HTTP payloads."""


def require(condition: Any, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class API:
    def __init__(self, base_url: str, origin: str | None = None):
        self.base = base_url.rstrip("/")
        if not self.base.endswith("/api/v1"):
            self.base += "/api/v1"
        parsed = urlsplit(self.base)
        require(parsed.scheme in {"http", "https"} and not parsed.username and not parsed.password, "base URL must be HTTP(S) and contain no credentials")
        self.origin = origin or f"{parsed.scheme}://{parsed.netloc}"
        self.client = httpx.Client(base_url=self.base + "/", headers={"Origin": self.origin}, timeout=httpx.Timeout(30, connect=10), follow_redirects=False, trust_env=False)

    def request(self, method: str, path: str, expected: tuple[int, ...] = (200,), **kwargs: Any) -> Any:
        response = self.client.request(method, path.lstrip("/"), **kwargs)
        if response.status_code not in expected:
            code = ""
            try:
                candidate = response.json().get("error", {}).get("code", "")
                if isinstance(candidate, str) and candidate.replace("_", "").isalnum():
                    code = f" ({candidate[:60]})"
            except (ValueError, AttributeError):
                pass
            raise CheckFailure(f"{method} {path.split('?')[0]} returned HTTP {response.status_code}{code}; expected {expected}")
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.content

    def login(self, username: str, password: str) -> dict[str, Any]:
        user = self.request("POST", "/auth/login", json={"username": username, "password": password})
        require(isinstance(user, dict) and user.get("id"), "login did not return a User")
        return user

    def close(self) -> None:
        self.client.close()


def parse_sse(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    payload: list[str] = []
    event_id: str | None = None
    event_type: str | None = None
    for line in lines:
        if not line:
            if payload:
                try:
                    event = json.loads("\n".join(payload))
                except json.JSONDecodeError as exc:
                    raise CheckFailure("SSE data was not valid JSON") from exc
                require(isinstance(event, dict), "SSE data must be an object")
                if event_id is not None:
                    require(str(event.get("id")) == event_id, "SSE ID does not match payload ID")
                if event_type:
                    require(event.get("type") == event_type, "SSE type does not match payload type")
                yield event
            payload, event_id, event_type = [], None, None
        elif line.startswith(":"):
            continue
        elif line.startswith("data:"):
            payload.append(line[5:].lstrip(" "))
        elif line.startswith("id:"):
            event_id = line[3:].strip()
        elif line.startswith("event:"):
            event_type = line[6:].strip()


def tool_matches(name: Any, suffix: str) -> bool:
    return isinstance(name, str) and (name == suffix or name.endswith("__" + suffix))


class Smoke:
    def __init__(self, args: argparse.Namespace, username: str, password: str):
        self.args = args
        self.username, self.password = username, password
        self.tag = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
        self.admin = API(args.base_url, args.origin)
        self.member = API(args.base_url, args.origin)
        self.other = API(args.base_url, args.origin)
        self.users: list[str] = []
        self.connection_id: str | None = None
        self.assistant_id: str | None = None
        self.conversations: list[str] = []
        self.runs: list[str] = []
        self.report: dict[str, Any] = {"started_at": now(), "tag": self.tag, "real_model_requested": args.real_model, "checks": [], "resources": {}, "limitations": ["Synthetic MCP data only; customer business MCP and production identity integration are not validated by this script.", "A successful API run is not a browser visual acceptance or a server-restart recovery test."]}

    def check(self, name: str, operation: Callable[[], Any]) -> Any:
        started = time.monotonic()
        try:
            result = operation()
        except Exception as exc:
            detail = str(exc) if isinstance(exc, CheckFailure) else type(exc).__name__
            self.report["checks"].append({"name": name, "status": "failed", "seconds": round(time.monotonic() - started, 2), "detail": detail})
            print(f"FAIL {name}: {detail}", flush=True)
            raise
        self.report["checks"].append({"name": name, "status": "passed", "seconds": round(time.monotonic() - started, 2)})
        print(f"PASS {name}", flush=True)
        return result

    def bootstrap(self) -> None:
        config = self.admin.request("GET", "/config")
        require(config.get("brand_name"), "public config has no brand name")
        self.admin.request("GET", "/health/ready")
        user = self.admin.login(self.username, self.password)
        require(user.get("role") == "admin", "acceptance requires an administrator account")
        require(self.admin.request("GET", "/auth/me")["id"] == user["id"], "login cookie does not authenticate subsequent requests")
        for index, client in enumerate((self.member, self.other)):
            member_name = f"smoke-{self.tag}-{index}"
            member_password = secrets.token_urlsafe(30)
            created = self.admin.request("POST", "/users", expected=(200, 201), json={"username": member_name, "password": member_password, "display_name": f"验收成员 {index + 1}", "role": "member"})
            self.users.append(created["id"])
            client.login(member_name, member_password)
        self.report["resources"]["user_ids"] = list(self.users)

    def setup_connection(self) -> None:
        token = os.environ.get("DEMO_MCP_TOKEN", "")
        payload: dict[str, Any] = {"name": f"演示验收连接 {self.tag}", "transport": "http", "url": self.args.mcp_url, "auth_type": "bearer" if token else "none", "enabled": True}
        if token:
            payload["credential"] = token
        connection = self.admin.request("POST", "/connections", expected=(200, 201), json=payload)
        self.connection_id = connection["id"]
        require("credential" not in connection, "connection API returned a credential field")
        if token:
            require(token not in json.dumps(connection), "connection API exposed its token")
        tested = self.admin.request("POST", f"/connections/{self.connection_id}/test", timeout=60)
        require(tested.get("status") == "connected", "demo MCP connection test did not connect")
        names = {tool["name"] for tool in tested.get("tools", [])}
        require(REQUIRED_TOOLS.issubset(names), "demo MCP discovery omitted required tools")
        current = self.admin.request("GET", f"/connections/{self.connection_id}")
        require(current.get("name") == payload["name"], "connection was not persisted")
        self.member.request("POST", "/connections", expected=(403,), json=payload)
        self.report["resources"]["connection_id"] = self.connection_id

    def setup_assistant(self) -> None:
        assistant = self.admin.request("POST", "/assistants", expected=(200, 201), json={
            "name": f"演示 QA 验收 {self.tag}",
            "description": "仅连接合成演示知识和演示工单，供验收使用。",
            "instructions": "你是验收助手。按用户要求调用已配置工具，不能凭空编造查询结果。所有数据都是演示数据。创建工单必须使用 create_ticket 工具，保留用户给定的 request_key、title、description、requester。工具拒绝后不要重试写入；如实说明未创建。",
            "enabled": True,
            "access": {"all_members": False, "user_ids": [self.users[0]]},
            "connections": [{"connection_id": self.connection_id, "allowed_tools": sorted(REQUIRED_TOOLS), "auto_approved_tools": ["search_knowledge", "list_tickets", "slow_lookup"]}],
        })
        self.assistant_id = assistant["id"]
        visible = self.member.request("GET", "/assistants")
        require(any(item["id"] == self.assistant_id for item in visible), "authorized member cannot see assistant")
        hidden = self.other.request("GET", "/assistants")
        require(not any(item["id"] == self.assistant_id for item in hidden), "unauthorized member can see restricted assistant")
        self.other.request("POST", "/conversations", expected=(403, 404), json={"assistant_id": self.assistant_id})
        self.report["resources"]["assistant_id"] = self.assistant_id

    def conversation(self, suffix: str) -> str:
        created = self.member.request("POST", "/conversations", expected=(200, 201), json={"assistant_id": self.assistant_id, "title": f"验收 {suffix} {self.tag}"})
        self.conversations.append(created["id"])
        return created["id"]

    def ownership_and_files(self) -> None:
        conversation_id = self.conversation("资源权限")
        self.member.request("PATCH", f"/conversations/{conversation_id}", json={"title": f"已修改的验收会话 {self.tag}"})
        for path in (f"/conversations/{conversation_id}", f"/conversations/{conversation_id}/messages"):
            self.other.request("GET", path, expected=(403, 404))
            self.admin.request("GET", path, expected=(403, 404))
        content = f"Synthetic attachment. Acceptance tag: {self.tag}".encode()
        uploaded = self.member.request("POST", "/files", expected=(200, 201), files={"file": ("acceptance-note.txt", content, "text/plain")})
        file_id = uploaded["id"]
        downloaded = self.member.request("GET", f"/files/{file_id}/content")
        require(downloaded == content, "downloaded attachment bytes differ from upload")
        self.other.request("GET", f"/files/{file_id}/content", expected=(403, 404))
        self.admin.request("GET", f"/files/{file_id}/content", expected=(403, 404))
        self.report["resources"]["attachment_id"] = file_id
        self.member.request("PATCH", f"/conversations/{conversation_id}", json={"archived": True})

    def send(self, conversation_id: str, text: str) -> str:
        payload = {"client_request_id": str(uuid.uuid4()), "text": text, "file_ids": []}
        result = self.member.request("POST", f"/conversations/{conversation_id}/messages", expected=(202,), json=payload)
        run_id = result["run_id"]
        self.runs.append(run_id)
        duplicate = self.member.request("POST", f"/conversations/{conversation_id}/messages", expected=(200, 202), json=payload)
        require(duplicate["run_id"] == run_id, "same client_request_id started another run")
        self.other.request("GET", f"/runs/{run_id}", expected=(403, 404))
        return run_id

    def consume_run(self, run_id: str, *, decision: str | None = None, expected_key: str | None = None, cancel_tool: str | None = None, reconnect_once: bool = False) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        last_id = 0
        reconnect_done = not reconnect_once
        cancelled = False
        approvals = 0
        started = time.monotonic()
        while time.monotonic() - started < self.args.timeout:
            forced_disconnect = False
            with self.member.client.stream("GET", f"runs/{run_id}/events", params={"after": last_id}, headers={"Accept": "text/event-stream", "Last-Event-ID": str(last_id)}, timeout=httpx.Timeout(35, connect=10)) as response:
                require(response.status_code == 200, f"run event stream returned HTTP {response.status_code}")
                require("text/event-stream" in response.headers.get("content-type", ""), "run events are not SSE")
                for event in parse_sse(response.iter_lines()):
                    require(event.get("run_id") == run_id, "SSE event belongs to another run")
                    event_id = int(event.get("id", 0))
                    require(event_id > last_id, "SSE replay duplicated or reordered an event")
                    last_id = event_id
                    events.append(event)
                    data = event.get("data", {})
                    if event.get("type") == "approval.required":
                        approvals += 1
                        approval_id = data.get("id") or data.get("approval_id")
                        require(approval_id, "approval event omitted its ID")
                        permitted = decision in {"allow", "deny"} and tool_matches(data.get("tool_name"), "create_ticket") and data.get("arguments", {}).get("request_key") == expected_key and approvals <= 3
                        selected = decision if permitted else "deny"
                        self.other.request("POST", f"/approvals/{approval_id}/decision", expected=(403, 404), json={"decision": "allow"})
                        self.member.request("POST", f"/approvals/{approval_id}/decision", json={"decision": selected})
                        require(permitted, "unexpected approval was safely denied")
                    if cancel_tool and event.get("type") == "tool.started" and tool_matches(data.get("tool_name"), cancel_tool) and not cancelled:
                        self.member.request("POST", f"/runs/{run_id}/cancel", expected=(200, 202))
                        cancelled = True
                    if event.get("type") == "run.completed":
                        final = self.member.request("GET", f"/runs/{run_id}")
                        require(final.get("status") in TERMINAL, "completed event arrived before persisted terminal run state")
                        if cancel_tool:
                            require(cancelled and final["status"] in {"cancelled", "interrupted"}, "cancel did not stop the active run")
                        else:
                            require(final["status"] == "succeeded", "real-model run did not succeed; inspect protected server diagnostics")
                        if decision:
                            require(approvals >= 1, "write action never requested approval")
                        return events
                    if not reconnect_done:
                        reconnect_done = True
                        forced_disconnect = True
                        break
                    require(time.monotonic() - started < self.args.timeout, "run exceeded acceptance timeout")
            if not forced_disconnect:
                state = self.member.request("GET", f"/runs/{run_id}")
                require(state.get("status") not in TERMINAL, "SSE ended without a run.completed event")
                time.sleep(0.25)
        raise CheckFailure("run exceeded acceptance timeout")

    def read_model(self) -> None:
        conversation_id = self.conversation("真实模型读取")
        run_id = self.send(conversation_id, "请调用 search_knowledge 工具查询演示设备报修流程，回答普通故障的响应时间，并给出知识条目编号。不要创建工单。")
        events = self.consume_run(run_id, reconnect_once=True)
        require(any(event["type"] == "tool.started" and tool_matches(event.get("data", {}).get("tool_name"), "search_knowledge") for event in events), "model did not invoke the knowledge MCP tool")
        messages = self.member.request("GET", f"/conversations/{conversation_id}/messages")
        answers = [item for item in messages if item.get("role") == "assistant"]
        require(any("4" in item.get("text", "") and "DEMO-KB-001" in item.get("text", "") for item in answers), "answer did not contain the known response time and source ID")
        # Replay a completed event range using only Last-Event-ID.
        cursor = events[-2]["id"] if len(events) > 1 else 0
        with self.member.client.stream("GET", f"runs/{run_id}/events", headers={"Last-Event-ID": str(cursor)}) as response:
            require(response.status_code == 200, "terminal event replay failed")
            replay = list(parse_sse(response.iter_lines()))
        require(replay and all(int(event["id"]) > int(cursor) for event in replay), "Last-Event-ID replay did not honor its cursor")
        require(replay[-1]["type"] == "run.completed", "terminal replay omitted completion")
        self.report["resources"]["read_run_id"] = run_id

    def write_model(self, decision: str) -> None:
        key = f"acceptance-{self.tag}-{decision}"
        before = asyncio.run(read_demo_tickets(self.args.mcp_url, key))
        require(not before, "acceptance request key already exists")
        conversation_id = self.conversation(f"写入{decision}")
        ticket = {"title": f"验收演示设备故障 {decision}", "description": "合成数据：演示打印机无法启动，不是真实报修。", "requester": "验收脚本", "request_key": key}
        run_id = self.send(conversation_id, "请调用 create_ticket 创建一个演示工单。参数严格采用下面 JSON，不要改字段，等待系统的人工审批；被拒绝后直接说明未创建，不要重试。参数：" + json.dumps(ticket, ensure_ascii=False))
        self.consume_run(run_id, decision=decision, expected_key=key)
        after = asyncio.run(read_demo_tickets(self.args.mcp_url, key))
        if decision == "allow":
            require(len(after) == 1 and all(after[0].get(field) == value for field, value in ticket.items()), "approved write did not create exactly the requested demo ticket")
        else:
            require(not after, "denied write created a demo ticket")
        self.report["resources"][f"write_{decision}_run_id"] = run_id
        self.report["resources"][f"write_{decision}_request_key"] = key

    def cancel_model(self) -> None:
        conversation_id = self.conversation("中断")
        run_id = self.send(conversation_id, "请调用 slow_lookup 工具，delay_seconds 设置为 25；等待结果后用一句话回答。不要调用其他工具。")
        self.consume_run(run_id, cancel_tool="slow_lookup")
        self.report["resources"]["cancel_run_id"] = run_id

    def usage(self) -> None:
        member_usage = self.member.request("GET", "/usage")
        admin_usage = self.admin.request("GET", "/usage")
        require(isinstance(member_usage.get("total_runs"), int), "usage response omitted total_runs")
        require(admin_usage.get("total_runs", -1) >= member_usage["total_runs"], "admin usage total is below member total")
        cost = member_usage.get("estimated_cost_usd")
        require(cost is None or (isinstance(cost, (float, int)) and cost >= 0), "usage returned invalid estimated cost")
        self.report["usage"] = {key: member_usage.get(key) for key in ("total_runs", "successful_runs", "input_tokens", "output_tokens", "estimated_cost_usd", "currency")}

    def cleanup(self) -> None:
        failures = 0
        for run_id in self.runs:
            try:
                state = self.member.request("GET", f"/runs/{run_id}")
                if state.get("status") not in TERMINAL:
                    self.member.request("POST", f"/runs/{run_id}/cancel", expected=(200, 202))
            except Exception:
                failures += 1
        if not self.args.keep_resources:
            for conversation_id in self.conversations:
                try:
                    self.member.request("PATCH", f"/conversations/{conversation_id}", json={"archived": True})
                except Exception:
                    failures += 1
            if self.assistant_id:
                try:
                    self.admin.request("DELETE", f"/assistants/{self.assistant_id}", expected=(200, 204))
                except Exception:
                    failures += 1
            if self.connection_id:
                try:
                    self.admin.request("DELETE", f"/connections/{self.connection_id}", expected=(200, 204))
                except Exception:
                    failures += 1
            for user_id in self.users:
                try:
                    self.admin.request("PATCH", f"/users/{user_id}", json={"enabled": False})
                except Exception:
                    failures += 1
        self.report["cleanup"] = {"resources_retained": self.args.keep_resources, "errors": failures, "note": "Synthetic tickets and uploaded attachment bytes remain as acceptance evidence; no unrelated resource is deleted."}
        for client in (self.admin, self.member, self.other):
            client.close()

    def run(self) -> bool:
        passed = False
        try:
            self.check("login_and_member_accounts", self.bootstrap)
            self.check("mcp_connection_and_tool_discovery", self.setup_connection)
            self.check("assistant_binding_and_access", self.setup_assistant)
            self.check("conversation_file_ownership_and_crud", self.ownership_and_files)
            if self.args.real_model:
                self.check("real_model_read_and_sse_reconnect_replay", self.read_model)
                self.check("real_model_write_approved", lambda: self.write_model("allow"))
                self.check("real_model_write_denied", lambda: self.write_model("deny"))
                self.check("real_model_cancel", self.cancel_model)
            else:
                self.report["limitations"].append("Real inference, tool approval/denial and cancellation were NOT exercised; add --real-model to request those checks.")
            self.check("usage_visibility", self.usage)
            passed = True
        except Exception:
            pass
        finally:
            self.cleanup()
            self.report["finished_at"] = now()
            self.report["status"] = "passed" if passed and not self.report["cleanup"]["errors"] else "failed"
            self.report["resources"]["conversation_ids"] = self.conversations
            self.report["resources"]["run_ids"] = self.runs
            output = Path(self.args.report)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"Report: {output}", flush=True)
        return self.report["status"] == "passed"


async def read_demo_tickets(url: str, request_key: str) -> list[dict[str, Any]]:
    # Verify the business-side effect through the real MCP, not model prose.
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": "Bearer " + os.environ["DEMO_MCP_TOKEN"]} if os.environ.get("DEMO_MCP_TOKEN") else None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        async with streamablehttp_client(url, headers=headers, timeout=20, sse_read_timeout=30) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("list_tickets", {"request_key": request_key})
                require(not result.isError, "direct MCP read returned an error")
                body = result.structuredContent
                if not isinstance(body, dict):
                    texts = [item.text for item in result.content if getattr(item, "type", None) == "text"]
                    body = json.loads("\n".join(texts))
                require(isinstance(body, dict) and isinstance(body.get("tickets"), list), "demo MCP returned an unexpected ticket list")
                return body["tickets"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:18080"))
    parser.add_argument("--origin", default=os.environ.get("SMOKE_ORIGIN"), help="Use the configured client origin when it differs from base URL")
    parser.add_argument("--mcp-url", default=os.environ.get("SMOKE_MCP_URL", "http://127.0.0.1:8767/mcp"))
    parser.add_argument("--username", default=os.environ.get("SMOKE_ADMIN_USERNAME", "admin"), help="Administrator username (not a secret)")
    credentials_source = parser.add_mutually_exclusive_group()
    credentials_source.add_argument("--credentials-file", type=Path, help="Owner-controlled JSON containing username and password")
    credentials_source.add_argument("--password-file", type=Path, help="Owner-controlled UTF-8 file containing only the administrator password")
    parser.add_argument("--real-model", action="store_true", help="Opt in to four real model turns and real demo MCP operations; incurs provider usage")
    parser.add_argument("--keep-resources", action="store_true", help="Retain clearly labeled acceptance members, assistant and connection for review")
    parser.add_argument("--timeout", type=float, default=240, help="Maximum seconds per model run")
    parser.add_argument("--report", default=".runtime/smoke-report.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    username = args.username
    password = os.environ.get("SMOKE_ADMIN_PASSWORD", "")
    if args.credentials_file:
        try:
            credentials = json.loads(args.credentials_file.read_text(encoding="utf-8"))
            username, password = credentials["username"], credentials["password"]
        except (OSError, ValueError, KeyError):
            print("Cannot read credentials file; expected private JSON with username and password.", file=sys.stderr)
            return 2
    elif args.password_file:
        try:
            password = args.password_file.read_text(encoding="utf-8").strip()
        except OSError:
            print("Cannot read administrator password file.", file=sys.stderr)
            return 2
    if not username or not password:
        print("Set SMOKE_ADMIN_PASSWORD or provide --credentials-file/--password-file. Never pass secrets as command-line arguments.", file=sys.stderr)
        return 2
    try:
        smoke = Smoke(args, username, password)
        return 0 if smoke.run() else 1
    except CheckFailure as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Acceptance initialization failed: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
