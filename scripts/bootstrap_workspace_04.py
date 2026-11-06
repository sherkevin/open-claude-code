"""Install reviewed starter resources through normal authenticated product APIs.

The company document is supplied explicitly, never included in release archives.
Existing conversations and user uploads are not changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import httpx


RULES = """# 业务流程参考

## 设备报修
发现设备故障后记录设备编号、故障现象和联系人。普通故障在 4 个工作小时内响应；影响业务的严重故障在 30 分钟内响应。创建工单前确认标题、描述和申请人，并由用户批准提交。

## 差旅报销
差旅结束后 10 个工作日内提交报销，附行程、发票和审批记录。住宿上限为每晚 500 元。

## 质量统计口径
合格率 = 合格件数 / 检验件数。跨批次统计时先分别求和，再相除；不能直接平均各批次合格率。检验件数为零时结果为空值，不等于零合格率。生产日期采用 Asia/Shanghai 时区。
"""

SKILLS = [
    {"name": "工业异常分析", "description": "查询质量和设备记录，统一口径，定位异常并提出下一步验证。", "instructions": "明确对象、时间、单位和筛选条件；从已授权数据工具读取事实，按需查知识库规范。先给关键结论和带来源的指标，再比较时段/批次。按数量加权汇总比例，区分缺失、零值和查询失败。将观测事实、可能原因、待验证假设分开，给出最少必要的验证动作。不得从相关性直接断言因果；所有外部事实引用工具返回的 source 标记。"},
    {"name": "有据报告", "description": "将数据库、文件和网页证据整理成可点击核对的业务报告。", "instructions": "先确认报告目标和时间范围，再按需读取授权数据、文档及已启用的网页搜索。输出简短结论、关键事实、计算依据和建议行动。每个引用事实使用实际工具返回的 [[source:ID]] 标记；不要编造位置、页码、字段或链接。报告区分已实现能力、规划和推测。若用户要求文件，生成可下载的 Markdown 报告，同时在聊天保留核心结论和来源。"},
    {"name": "公司方案解读", "description": "基于 Better AI 公司资料，解释定位、产品能力和行业合作方案。", "instructions": "先检索当前绑定的 Better AI 公司知识库，必要时读取相关原页。按用户对象解释公司定位、解决的问题、产品结构和合作分工。将材料中的方向、试点、计划与已交付事实明确区分，不补造融资、客户、认证或训练归属信息。回答简洁，每段事实使用真实 source 引用。"},
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--company-pdf", type=Path, required=True)
    parser.add_argument("--assistant-id", required=True)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    with httpx.Client(base_url=args.base_url.rstrip("/") + "/api/v1/", timeout=1200, trust_env=False) as client:
        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json() if response.content else None
        user = request("POST", "auth/login", json={"username": args.username, "password": args.password_file.read_text().strip()})
        if user["role"] != "admin":
            raise SystemExit("Starter team resources require an administrator")
        bases = request("GET", "knowledge-bases")
        def ensure_base(name, description, default=False):
            existing = next((item for item in bases if item["name"] == name and item["scope"] == "team"), None)
            if existing:
                return existing
            return request("POST", "knowledge-bases", json={"name": name, "description": description, "scope": "team", "default_for_new_assistants": default})
        company = ensure_base("Better AI 公司知识库", "公司介绍、产品方向和合作方案；以文件版本、原页和实际表述为准。", True)
        business = ensure_base("工业业务试用知识库", "独立的合成验收规则集，仅绑定试用助手；正式助手由用户选择自己的知识库。")
        def ensure_document(kb, name, content, mime):
            documents = request("GET", f"knowledge-bases/{kb['id']}/documents")
            digest = hashlib.sha256(content).hexdigest()
            existing = next((item for item in documents if item["sha256"] == digest), None)
            result = existing or request("POST", f"knowledge-bases/{kb['id']}/documents", files={"file": (name, content, mime)})
            if result["status"] != "ready":
                result = request("POST", f"knowledge-documents/{result['id']}/reindex")
            if result["status"] != "ready":
                raise SystemExit("Knowledge indexing failed; inspect the document status in the client")
            return result
        company_document = ensure_document(company, "Better AI第8版_客户展示版_v3.pdf", args.company_pdf.read_bytes(), "application/pdf")
        business_document = ensure_document(business, "业务流程参考.md", RULES.encode(), "text/markdown")
        existing_skills = request("GET", "skills")
        selected = []
        for skill in SKILLS:
            existing = next((item for item in existing_skills if item["name"] == skill["name"] and item["scope"] == "team"), None)
            selected.append(existing or request("POST", "skills", json={**skill, "scope": "team"}))
        connection = request("PATCH", f"connections/{args.connection_id}", json={"name": "业务试用数据连接"})
        tested = request("POST", f"connections/{args.connection_id}/test")
        available = {tool["name"] for tool in tested.get("tools", [])}
        if "query_quality_metrics" not in available:
            connection = request("GET", f"connections/{args.connection_id}")
            available = {tool["name"] for tool in connection.get("tools", [])}
        required = {"query_quality_metrics", "list_tickets", "create_ticket"}
        if not required.issubset(available):
            raise SystemExit("Restart the updated fixture MCP before configuring the assistant")
        assistant = request("PATCH", f"assistants/{args.assistant_id}", json={
            "name": "工业业务助手", "description": "查询业务知识和质量记录，分析异常，整理带来源的结果。",
            "instructions": "围绕设备、质量、工单和企业知识完成用户任务。按实际工具结果客观回答，以已绑定知识库和数据连接为依据。创建工单前确认提交内容并等待产品审批。公司信息先查询 Better AI 公司知识库。",
            "knowledge_base_ids": [company["id"], business["id"]], "skill_ids": [item["id"] for item in selected],
            "web_search_enabled": True, "default_model_id": "dano-v2.1", "default_reasoning_level": "high",
            "connections": [{"connection_id": args.connection_id, "allowed_tools": sorted(required), "auto_approved_tools": ["query_quality_metrics", "list_tickets"]}],
        })
        # The company corpus is shared by default, business fixtures remain isolated.
        for item in request("GET", "assistants?manage=true"):
            if item["id"] == assistant["id"] or not item["enabled"] or not item["can_edit"]:
                continue
            ids = item.get("knowledge_base_ids", [])
            if company["id"] not in ids:
                request("PATCH", f"assistants/{item['id']}", json={"knowledge_base_ids": [*ids, company["id"]]})
        report = {"company": company, "company_document": company_document, "business": business,
                  "business_document": business_document, "skill_ids": [item["id"] for item in selected], "assistant_id": assistant["id"], "connection_id": args.connection_id}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "ready", "company_pages": company_document["page_count"], "company_chunks": company_document["chunk_count"], "skills": len(selected)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
