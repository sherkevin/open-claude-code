"""Idempotently copy reviewed public/demo knowledge through the product API.

This is intentionally opt-in: normal service startup never creates data and
the release synchronizer never copies runtime databases.  The source and
target password files must stay outside the repository.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import httpx


CORPORA = {
    "工业质量知识库（示例）": "工业质量知识库（示例）",
    "多模态质量图谱（示例）": "多模态质量图谱（示例）",
    "工业公开知识库样例（NIST/OSHA/国标）": "工业公开知识库样例（NIST/OSHA/国标）",
    "工业公开多模态图谱样例（视觉向量+OCR）": "工业公开多模态图谱样例（视觉向量+OCR）",
    # The target already has a customer-owned company corpus; append only the
    # reviewed team documents from the source installation by content hash.
    "Better AI 公司知识库（内部）": "Better AI 公司知识库",
}


def request(client: httpx.Client, method: str, path: str, **kwargs):
    response = client.request(method, path, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path}: HTTP {response.status_code}: {response.text[:500]}")
    return response


def login(client: httpx.Client, password_file: Path) -> None:
    request(client, "POST", "auth/login", json={"username": "admin", "password": password_file.read_text(encoding="utf-8").strip()})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-base", required=True, help="Source API base, including /api/v1/")
    parser.add_argument("--target-base", required=True, help="Target API base, including /api/v1/")
    parser.add_argument("--source-password-file", type=Path, required=True)
    parser.add_argument("--target-password-file", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--insecure-target-tls", action="store_true", help="Only for a controlled preview with a self-signed certificate")
    parser.add_argument("--bind-assistant-name", default="工业业务助手")
    args = parser.parse_args()

    with (
        httpx.Client(base_url=args.source_base.rstrip("/") + "/", timeout=1800, trust_env=False) as source,
        httpx.Client(base_url=args.target_base.rstrip("/") + "/", timeout=1800, verify=not args.insecure_target_tls, trust_env=False) as target,
    ):
        login(source, args.source_password_file)
        login(target, args.target_password_file)
        source_kbs = {row["name"]: row for row in request(source, "GET", "knowledge-bases").json()}
        target_kbs = {row["name"]: row for row in request(target, "GET", "knowledge-bases").json()}
        report: list[dict] = []
        for source_name, target_name in CORPORA.items():
            source_kb = source_kbs.get(source_name)
            if not source_kb:
                raise RuntimeError(f"source knowledge base is missing: {source_name}")
            target_kb = target_kbs.get(target_name)
            if not target_kb:
                target_kb = request(target, "POST", "knowledge-bases", json={
                    "name": target_name,
                    "description": f"{source_kb.get('description', '')}（公开/合成样例，非客户生产数据）",
                    "scope": "team",
                    "default_for_new_assistants": False,
                }).json()
                target_kbs[target_name] = target_kb
            existing = {row.get("sha256") for row in request(target, "GET", f"knowledge-bases/{target_kb['id']}/documents").json()}
            copied, skipped = [], []
            for document in request(source, "GET", f"knowledge-bases/{source_kb['id']}/documents").json():
                if document.get("sha256") in existing:
                    skipped.append(document["name"])
                    continue
                content = request(source, "GET", document["content_url"].replace("/api/v1/", "")).content
                if hashlib.sha256(content).hexdigest() != document.get("sha256"):
                    raise RuntimeError(f"source digest mismatch: {source_name}/{document['name']}")
                result = request(target, "POST", f"knowledge-bases/{target_kb['id']}/documents", files={
                    "file": (document["name"], content, document.get("mime_type") or "application/octet-stream")
                }).json()
                copied.append({"name": document["name"], "sha256": document["sha256"], "status": result.get("status"),
                               "page_count": result.get("page_count"), "chunk_count": result.get("chunk_count")})
                existing.add(document.get("sha256"))
            report.append({"source": source_name, "target": target_name, "target_id": target_kb["id"],
                           "copied": copied, "skipped": skipped})

        assistants = request(target, "GET", "assistants?manage=true").json()
        assistant = next((row for row in assistants if row.get("name") == args.bind_assistant_name and row.get("enabled")), None)
        if assistant:
            ids = list(dict.fromkeys((assistant.get("knowledge_base_ids") or []) + [target_kbs[name]["id"] for name in CORPORA.values()]))
            request(target, "PATCH", f"assistants/{assistant['id']}", json={"knowledge_base_ids": ids})
            report.append({"assistant_id": assistant["id"], "assistant_name": assistant["name"], "bound_knowledge_base_count": len(ids)})

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "ready", "report": str(args.report)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
