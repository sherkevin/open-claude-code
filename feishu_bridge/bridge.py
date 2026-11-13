from __future__ import annotations

import logging
import queue
import re
import threading
import time
from dataclasses import dataclass

from .config import BridgeConfig
from .gateway import AgentGateway, GatewayError
from .parser import IncomingMessage, UnsupportedMessage, parse_message
from .transport import FeishuApiError, FeishuTransport

log = logging.getLogger("feishu_bridge")


@dataclass(frozen=True)
class Watch:
    run_id: str
    chat_id: str


class FeishuBridge:
    def __init__(self, config: BridgeConfig, gateway: AgentGateway | None = None, transport: FeishuTransport | None = None):
        self.config = config
        self.gateway = gateway or AgentGateway(config)
        self.transport = transport or FeishuTransport(config)
        self.incoming: queue.Queue[object] = queue.Queue(maxsize=100)
        self.stop = threading.Event()
        self.worker: threading.Thread | None = None
        self.watchers: dict[str, threading.Thread] = {}
        self._watch_lock = threading.Lock()

    def start_workers(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.worker = threading.Thread(target=self._worker_loop, name="feishu-message-worker", daemon=True)
        self.worker.start()

    def close(self) -> None:
        self.stop.set()
        try:
            self.incoming.put_nowait(None)
        except queue.Full:
            pass
        if self.worker:
            self.worker.join(timeout=5)
        self.gateway.close()
        self.transport.close()

    def on_event(self, raw_event) -> None:
        """Fast SDK callback: parse only enough to enqueue and return."""
        try:
            message = parse_message(raw_event)
        except UnsupportedMessage as exc:
            log.info("ignored Feishu event: %s", exc)
            return
        try:
            self.incoming.put_nowait(message)
        except queue.Full:
            # Raising here would make Feishu retry a message while the bridge
            # is saturated.  The durable event idempotency gate on the server
            # makes a later retry safe, so drop this delivery and log it.
            log.error("Feishu event queue is full; event will be retried: %s", message.event_id)

    def _worker_loop(self) -> None:
        while not self.stop.is_set():
            try:
                item = self.incoming.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                return
            try:
                self._handle(item)
            except Exception:
                log.exception("failed to handle Feishu message")

    def _reply(self, message: IncomingMessage, text: str) -> None:
        try:
            self.transport.reply(message.message_id, text)
        except Exception:
            log.exception("failed to reply to Feishu message %s", message.message_id)

    def _send(self, chat_id: str, text: str) -> None:
        try:
            self.transport.send_chat(chat_id, text)
        except Exception:
            log.exception("failed to send Feishu notification to %s", chat_id)

    def _handle(self, message: IncomingMessage) -> None:
        if message.text.startswith("/"):
            self._command(message)
            return
        try:
            result = self.gateway.submit(message)
        except GatewayError as exc:
            self._reply(message, f"提交失败：{exc}")
            return
        run_id = result.get("run_id")
        if not run_id:
            self._reply(message, "服务端没有返回任务编号，请稍后重试。")
            return
        state = "任务已接收" if result.get("duplicate") is not True else "这条消息已经提交过"
        self._reply(message, f"{state}。任务编号：{str(run_id)[:12]}\n可发送 /status 查看进度。")
        self._watch_run(str(run_id), message.chat_id)

    def _command(self, message: IncomingMessage) -> None:
        parts = message.text.strip().split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) == 2 else ""
        if command in {"/help", "/帮助"}:
            self._reply(message, "可用命令：\n/status 查看任务\n/stop [任务编号] 停止任务\n/approve <审批编号>\n/deny <审批编号>\n直接发送文字即可创建任务。")
            return
        if command in {"/status", "/状态"}:
            try:
                rows = self.gateway.status()
            except GatewayError as exc:
                self._reply(message, f"查询失败：{exc}")
                return
            if not rows:
                self._reply(message, "当前没有执行中的任务。")
                return
            lines = ["执行中的任务："]
            for item in rows[:10]:
                run = item.get("run", {})
                lines.append(f"- {str(run.get('id', ''))[:12]}：{run.get('status', '未知')}，{run.get('model', '模型未知')}")
            self._reply(message, "\n".join(lines))
            return
        if command in {"/stop", "/停止"}:
            self._stop_command(message, argument)
            return
        if command in {"/approve", "/允许", "/deny", "/拒绝"}:
            approval_id = argument.split(maxsplit=1)[0] if argument else ""
            if not approval_id:
                self._reply(message, "请提供审批编号，例如：/approve abc123")
                return
            decision = "allow" if command in {"/approve", "/允许"} else "deny"
            try:
                self.gateway.decide(approval_id, decision)
            except GatewayError as exc:
                self._reply(message, f"审批未处理：{exc}")
                return
            self._reply(message, "已记录审批决定：允许。" if decision == "allow" else "已记录审批决定：拒绝。")
            return
        self._reply(message, "未识别命令，发送 /help 查看用法。")

    def _stop_command(self, message: IncomingMessage, argument: str) -> None:
        run_id = argument
        if not run_id:
            try:
                rows = self.gateway.status()
            except GatewayError as exc:
                self._reply(message, f"查询失败：{exc}")
                return
            same_chat = [watch.run_id for watch in self._watches_for_chat(message.chat_id)]
            candidates = [str(item.get("run", {}).get("id")) for item in rows if item.get("run", {}).get("id") in same_chat]
            run_id = candidates[-1] if candidates else (str(rows[0].get("run", {}).get("id")) if len(rows) == 1 else "")
        if not run_id:
            self._reply(message, "找不到要停止的任务，请提供完整任务编号。")
            return
        try:
            self.gateway.cancel(run_id)
        except GatewayError as exc:
            self._reply(message, f"停止失败：{exc}")
            return
        self._reply(message, f"已请求停止任务 {run_id[:12]}。")

    def _watches_for_chat(self, chat_id: str) -> list[Watch]:
        with self._watch_lock:
            return [Watch(run_id, chat_id) for run_id, thread in self.watchers.items() if thread.is_alive() and getattr(thread, "chat_id", chat_id) == chat_id]

    def _watch_run(self, run_id: str, chat_id: str) -> None:
        with self._watch_lock:
            existing = self.watchers.get(run_id)
            if existing and existing.is_alive():
                return
            thread = threading.Thread(target=self._watch_loop, args=(run_id, chat_id), name=f"feishu-run-{run_id[:8]}", daemon=True)
            # Used only by _watches_for_chat; assigning a plain attribute keeps
            # the thread implementation independent of the transport.
            thread.chat_id = chat_id  # type: ignore[attr-defined]
            self.watchers[run_id] = thread
            thread.start()

    def _watch_loop(self, run_id: str, chat_id: str) -> None:
        cursor = 0
        last_progress = 0.0
        try:
            while not self.stop.is_set():
                try:
                    snapshot = self.gateway.run_events(run_id, cursor)
                except GatewayError as exc:
                    log.warning("run %s status poll failed: %s", run_id, exc)
                    time.sleep(min(10.0, self.config.poll_seconds * 4))
                    continue
                for event in snapshot.get("events", []):
                    cursor = max(cursor, int(event.get("id", cursor)))
                    kind = event.get("type")
                    data = event.get("data") or {}
                    if kind == "approval.required":
                        approval_id = str(data.get("id", ""))
                        tool = str(data.get("tool_name", "未知工具"))
                        self._send(chat_id, f"任务需要审批。工具：{tool}\n审批编号：{approval_id}\n发送 /approve {approval_id} 或 /deny {approval_id}")
                    elif kind == "run.stage" and time.time() - last_progress >= self.config.progress_seconds:
                        stage = str(data.get("stage", "执行中"))
                        self._send(chat_id, f"任务 {run_id[:12]}：{stage}")
                        last_progress = time.time()
                run = snapshot.get("run") or {}
                if run.get("status") in {"succeeded", "failed", "cancelled", "interrupted"}:
                    message = snapshot.get("assistant_message") or {}
                    text = str(message.get("text") or "").strip()
                    if run.get("status") == "succeeded":
                        self._send(chat_id, text or "任务已完成，但没有返回正文。")
                    else:
                        self._send(chat_id, f"任务{run.get('status')}：{run.get('error') or '服务未返回具体原因'}")
                    return
                time.sleep(self.config.poll_seconds)
        finally:
            with self._watch_lock:
                self.watchers.pop(run_id, None)


def run_forever(config: BridgeConfig) -> None:
    config.validate()
    bridge = FeishuBridge(config)
    bridge.start_workers()
    try:
        import lark_oapi as lark
    except ImportError as exc:  # pragma: no cover - exercised in deployment
        bridge.close()
        raise RuntimeError("install feishu_bridge/requirements.txt before starting the bridge") from exc

    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(bridge.on_event).build()
    backoff = 2.0
    try:
        while not bridge.stop.is_set():
            try:
                client = lark.ws.Client(config.app_id, config.app_secret, event_handler=handler, auto_reconnect=True)
                log.info("starting Feishu WebSocket client")
                client.start()
                backoff = 2.0
            except KeyboardInterrupt:
                break
            except Exception:
                log.exception("Feishu WebSocket client stopped; reconnecting")
                bridge.stop.wait(backoff)
                backoff = min(60.0, backoff * 2)
    finally:
        bridge.close()
