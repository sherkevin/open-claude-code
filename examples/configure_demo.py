"""Configure the optional synthetic demo through normal admin APIs; no model call."""
import argparse
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--origin", default="http://127.0.0.1:18081")
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:8767/mcp")
    args = parser.parse_args()
    with httpx.Client(base_url=args.base_url.rstrip("/") + "/api/v1/", headers={"Origin": args.origin}, timeout=30) as client:
        def api(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json() if response.content else None
        api("POST", "auth/login", json={"username": args.username, "password": args.password_file.read_text().strip()})
        name = "演示业务连接"
        existing = [row for row in api("GET", "connections") if row["name"] == name]
        if existing:
            connection = existing[0]
            if connection["url"] != args.mcp_url or not connection["enabled"]:
                raise SystemExit("Existing demo connection differs; review it in the administrator UI")
        else:
            connection = api("POST", "connections", json={"name": name, "transport": "http", "url": args.mcp_url, "auth_type": "none", "enabled": True})
        tested = api("POST", f"connections/{connection['id']}/test")
        tools = {tool["name"] for tool in tested.get("tools", [])}
        if tested["status"] != "connected" or not {"search_knowledge", "list_tickets", "create_ticket"} <= tools:
            raise SystemExit("The configured MCP does not expose the expected synthetic demo tools")
        name = "业务演示助手"
        existing = [row for row in api("GET", "assistants") if row["name"] == name]
        if existing:
            assistant = existing[0]
        else:
            assistant = api("POST", "assistants", json={
                "name": name,
                "description": "使用合成资料演示知识查询、报修流程与审批；不连接华微真实业务数据。",
                "instructions": "你是业务演示助手。所有工具数据都是合成演示数据，请明确说明。查询必须使用工具并标注知识条目编号，不能编造来源。创建演示工单前确认用户给出的标题、描述和申请人；调用create_ticket时提供唯一request_key，写操作必须等待用户审批。拒绝后不要重试写入。",
                "enabled": True,
                "access": {"all_members": True, "user_ids": []},
                "connections": [{"connection_id": connection["id"], "allowed_tools": ["search_knowledge", "list_tickets", "create_ticket"], "auto_approved_tools": ["search_knowledge", "list_tickets"]}],
            })
        print("Synthetic demo configured. Assistant ID:", assistant["id"])
        print("Read-only tools auto-approved; create_ticket requires approval. No model request sent.")


if __name__ == "__main__":
    main()
