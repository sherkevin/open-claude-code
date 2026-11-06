"""Exercise production Service/SessionActor/SDKRuntime against a loopback provider.

Uses no real provider credentials or inference. Reports only controlled fixture
identities and hashes, never full tool definitions, runtime paths or headers.
Run with the server's pinned virtualenv Python on the target platform.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from app.config import Settings
from app.identity import build_system_prompt
from app.runtime import RunManager
from app.service import Service


async def probe():
    captured, expected = [], {}
    labels = {"dano-v2.1": "Dano-v2.1", "deepseek-v4.1-flash": "deepseek-v4.1-flash", "glm-5.3": "GLM-5.3"}
    instructions = "请完成合成验收，不调用工具，不写长期记忆。"
    policies = {identity: build_system_prompt(public_model_id=identity, public_model_label=label, assistant_instructions=instructions) for identity, label in labels.items()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            system = body.get("system", "")
            system_text = system if isinstance(system, str) else "\n".join(block.get("text", "") for block in system if isinstance(block, dict))
            identity = expected.get("identity")
            identity_heads = {key: policy.split("\n", 1)[0][:80] for key, policy in policies.items()}
            message_mentions = []
            for index, message in enumerate(body.get("messages", [])):
                text = json.dumps(message.get("content", ""), ensure_ascii=False)
                mentions = [label for label in labels.values() if label in text]
                policy_matches = [key for key, policy in policies.items() if policy in text]
                if mentions or policy_matches:
                    message_mentions.append({"index": index, "role": message.get("role"), "public_label_mentions": mentions, "full_policy_matches": policy_matches})
            captured.append({"path": self.path, "model": body.get("model"), "output_config": body.get("output_config"),
                             "requested_public_identity": identity,
                             "system_policy_matches": [key for key, policy in policies.items() if policy in system_text],
                             "system_identity_head_matches": [key for key, head in identity_heads.items() if head in system_text],
                             "system_contains_expected_policy": bool(identity and policies[identity] in system_text),
                             "system_sha256": hashlib.sha256(system_text.encode()).hexdigest(),
                             "system_characters": len(system_text), "message_count": len(body.get("messages", [])),
                             "message_identity_mentions": message_mentions})
            if "count_tokens" in self.path:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"input_tokens":42}')
                return
            reply = labels.get(identity, "fixture") + " ROUTING-PROBE-MARKER"
            response = {"id": f"msg_fixture_routing_{len(captured)}", "type": "message", "role": "assistant", "model": body.get("model"),
                        "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if body.get("stream") else "application/json")
            self.end_headers()
            if body.get("stream"):
                events = [("message_start", {"type": "message_start", "message": response}),
                          ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                          ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": reply}}),
                          ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                          ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 4}}),
                          ("message_stop", {"type": "message_stop"})]
                self.wfile.write("".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode())
            else:
                response.update(content=[{"type": "text", "text": reply}], stop_reason="end_turn")
                self.wfile.write(json.dumps(response).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    report = {"probe": "production-runtime-loopback-provider", "real_inference": False, "runs": []}
    try:
        with tempfile.TemporaryDirectory(prefix="runtime-routing-probe-") as temporary:
            root = Path(temporary)
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps({"default_model_id": "dano-v2.1", "models": [
                {"id": "dano-v2.1", "label": "Dano-v2.1", "upstream_model": "deepseek-v4.1-flash-1017", "reasoning_levels": {"light": "low", "high": "high", "max": "max"}, "default_reasoning_level": "high"},
                {"id": "deepseek-v4.1-flash", "label": "deepseek-v4.1-flash", "upstream_model": "deepseek-v4.1-flash"},
                {"id": "glm-5.3", "label": "GLM-5.3", "upstream_model": "glm-5.3"},
            ]}), encoding="utf-8")
            settings = Settings(data_dir=root / "data", model_catalog=str(catalog), provider_settings="", model="fixture", idle_seconds=120)
            settings.provider_env = lambda: {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}", "ANTHROPIC_API_KEY": "fake-loopback-key", "ANTHROPIC_AUTH_TOKEN": ""}
            service = Service(settings)
            manager = service.manager = RunManager(service)
            try:
                user = service.create_user({"username": "fixture", "display_name": "Fixture", "password": "fake-loopback-password", "role": "admin"})
                assistant = service.save_assistant({"name": "Routing fixture", "instructions": instructions, "connections": []})
                conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "Synthetic routing"}, user)
                session_ids = []
                for index, (identity, effort) in enumerate([("dano-v2.1", "high"), ("dano-v2.1", "light"), ("dano-v2.1", "max"), ("deepseek-v4.1-flash", "default"), ("glm-5.3", "default")]):
                    expected["identity"] = identity
                    if index:
                        service.patch_conversation(conversation["id"], {"model_id": identity, "reasoning_level": effort}, user)
                    offset = len(captured)
                    result, _ = service.prepare_run(conversation["id"], {"text": "请回复会话校验码 ROUTING-PROBE-MARKER 与当前公开模型名称，不调用工具。", "file_ids": [], "client_request_id": f"routing-probe-{index}"}, user)
                    manager.submit(result["run_id"])
                    deadline = asyncio.get_running_loop().time() + 120
                    while True:
                        run = service.require("runs", result["run_id"])
                        if run["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                            break
                        if asyncio.get_running_loop().time() > deadline:
                            raise RuntimeError("Loopback production runtime timed out")
                        await asyncio.sleep(0.05)
                    session_ids.append(service.require("conversations", conversation["id"])["sdk_session_id"])
                    wire = captured[offset:]
                    messages_wire = [item for item in wire if "count_tokens" not in item["path"]]
                    report["runs"].append({"model_id": identity, "reasoning_level": effort, "status": run["status"], "safe_error": run["error"], "wire": wire,
                                           "all_messages_use_selected_identity": bool(messages_wire) and all(item["system_contains_expected_policy"] and item["system_policy_matches"] == [identity] and item["system_identity_head_matches"] == [identity] for item in messages_wire)})
                report["same_native_session"] = bool(session_ids[0]) and len(set(session_ids)) == 1
                report["passed"] = report["same_native_session"] and all(run["status"] == "succeeded" and run["all_messages_use_selected_identity"] for run in report["runs"])
            finally:
                await manager.close()
                service.db.close()
    finally:
        server.shutdown()
        server.server_close()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(probe())
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, flush=True)
    raise SystemExit(0 if result["passed"] else 1)
