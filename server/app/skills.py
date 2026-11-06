"""Versioned, explicit instruction capabilities; never grants executable tools."""
from __future__ import annotations

import json
import re
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import Field

from .db import dump, now, uid
from .models import InputModel
from .resources import choose_scope, scope_fields
from .security import AppError


class SkillCreate(InputModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    instructions: str = Field(min_length=1, max_length=50000)
    scope: Literal["private", "team"] = "private"
    enabled: bool = True
    required_tools: list[str] = Field(default_factory=list, max_length=100)
    resources: dict[str, str] = Field(default_factory=dict, max_length=20)


class SkillPatch(InputModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    instructions: str | None = Field(default=None, min_length=1, max_length=50000)
    scope: Literal["private", "team"] | None = None
    enabled: bool | None = None
    required_tools: list[str] | None = Field(default=None, max_length=100)
    resources: dict[str, str] | None = Field(default=None, max_length=20)


class SkillImport(InputModel):
    markdown: str = Field(min_length=1, max_length=60000)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    scope: Literal["private", "team"] = "private"


def skill_view(row, user=None):
    return {**{key: row[key] for key in ("id", "name", "description", "instructions", "revision", "created_at", "updated_at")}, "enabled": bool(row["enabled"]), "required_tools": json.loads(row["required_tools"]), "resources": json.loads(row["resources"]), **scope_fields(row, user)}


def save_skill(service, data, user, item_id=None):
    old = service.require_resource("skills", item_id, user, edit=True) if item_id else None
    scope, owner = choose_scope(data, old, user)
    if old:
        service.validate_resource_scope_change("skills", item_id, scope, owner)
    merged = {**(skill_view(old) if old else {"description": "", "enabled": True, "required_tools": [], "resources": {}}), **{k: v for k, v in data.items() if v is not None}}
    if any(not isinstance(name, str) or not name or len(name) > 256 for name in merged["required_tools"]) or any(not key or len(key) > 100 or len(value) > 20000 for key, value in merged["resources"].items()) or sum(len(v) for v in merged["resources"].values()) > 50000:
        raise AppError(422, "INVALID_SKILL", "Skill 工具声明或参考内容格式无效")
    if not merged["instructions"].strip():
        raise AppError(422, "INVALID_SKILL", "请填写 Skill 的使用说明")
    item_id, stamp = item_id or uid(), now()
    values = (merged["name"].strip(), merged["description"], merged["instructions"], dump(list(dict.fromkeys(merged["required_tools"]))), dump(merged["resources"]), owner, scope, merged["enabled"], stamp)
    with service.db.transaction():
        if old:
            service.db.execute("UPDATE skills SET name=?,description=?,instructions=?,required_tools=?,resources=?,owner_id=?,scope=?,enabled=?,updated_at=?,revision=revision+1 WHERE id=?", (*values, item_id))
        else:
            service.db.execute("INSERT INTO skills(name,description,instructions,required_tools,resources,owner_id,scope,enabled,updated_at,id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (*values, item_id, stamp))
        result = skill_view(service.require("skills", item_id), user)
        service.db.execute("INSERT INTO skill_versions VALUES(?,?,?,?)", (item_id, result["revision"], dump(result), stamp))
    return result


def import_skill(body):
    markdown = body.markdown.lstrip("\ufeff").replace("\r\n", "\n")
    meta, instructions = {}, markdown
    if markdown.startswith("---\n"):
        end = markdown.find("\n---", 4)
        if end < 0:
            raise AppError(422, "INVALID_SKILL_IMPORT", "SKILL.md 的开头元信息未闭合")
        header, instructions = markdown[4:end], markdown[end + 4:].strip()
        # Only simple display metadata is accepted, never executable YAML tags.
        current = None
        for line in header.splitlines():
            match = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
            if match:
                key, value = match.groups()
                current = key
                meta[key] = value.strip().strip("\"'") if value.strip() not in {"|", ">", "|-", ">-"} else ""
            elif current and line.startswith((" ", "\t")):
                meta[current] += " " + line.strip()
    heading = re.search(r"^#\s+(.+)$", instructions, re.MULTILINE)
    name = body.name or meta.get("name") or (heading.group(1) if heading else "新 Skill")
    declared = meta.get("allowed-tools", meta.get("required_tools", "")).strip("[]")
    required_tools = [item.strip("\"'") for item in re.split(r"[,\s]+", declared) if item and item != "-"]
    return SkillCreate(name=name.strip(), description=meta.get("description", "").strip(), instructions=instructions, scope=body.scope, required_tools=required_tools).model_dump()


def assistant_skills(service, assistant, user):
    ids = json.loads(assistant["skill_ids"]) if isinstance(assistant.get("skill_ids"), str) else assistant.get("skill_ids", [])
    result = []
    for item_id in ids:
        row = service.db.one("SELECT * FROM skills WHERE id=?", (item_id,))
        if service.resource_access(row, user) and row["enabled"]:
            result.append(skill_view(row, user))
    return result


def selected_skills(service, assistant, user, ids):
    available = {row["id"]: row for row in assistant_skills(service, assistant, user)}
    result = []
    for item_id in dict.fromkeys(ids):
        if item_id not in available:
            raise AppError(403, "SKILL_UNAVAILABLE", "所选 Skill 已停用或未配置给此助手，请重新选择")
        result.append(available[item_id])
    return result


def create_skill_router(service_dependency, authenticated):
    router = APIRouter(prefix="/api/v1", tags=["Skills"])

    @router.get("/skills")
    def listing(user=Depends(authenticated), service=Depends(service_dependency)):
        return [skill_view(row, user) for row in service.db.all("SELECT * FROM skills WHERE deleted=0 ORDER BY name") if service.resource_access(row, user)]

    @router.post("/skills", status_code=201)
    def create(body: SkillCreate, user=Depends(authenticated), service=Depends(service_dependency)):
        return save_skill(service, body.model_dump(), user)

    @router.post("/skills/import", status_code=201)
    def import_markdown(body: SkillImport, user=Depends(authenticated), service=Depends(service_dependency)):
        return save_skill(service, import_skill(body), user)

    @router.get("/skills/{item_id}")
    def get(item_id: str, user=Depends(authenticated), service=Depends(service_dependency)):
        return skill_view(service.require_resource("skills", item_id, user), user)

    @router.get("/skills/{item_id}/versions")
    def versions(item_id: str, user=Depends(authenticated), service=Depends(service_dependency)):
        service.require_resource("skills", item_id, user)
        return [{**json.loads(row["snapshot"]), **scope_fields(service.require("skills", item_id), user)} for row in service.db.all("SELECT * FROM skill_versions WHERE skill_id=? ORDER BY revision DESC", (item_id,))]

    @router.patch("/skills/{item_id}")
    def patch(item_id: str, body: SkillPatch, user=Depends(authenticated), service=Depends(service_dependency)):
        return save_skill(service, body.model_dump(exclude_unset=True), user, item_id)

    @router.delete("/skills/{item_id}", status_code=204)
    def delete(item_id: str, user=Depends(authenticated), service=Depends(service_dependency)):
        service.require_resource("skills", item_id, user, edit=True)
        service.db.execute("UPDATE skills SET deleted=1,enabled=0,revision=revision+1,updated_at=? WHERE id=?", (now(), item_id))

    @router.get("/assistants/{item_id}/skills")
    def available(item_id: str, user=Depends(authenticated), service=Depends(service_dependency)):
        return assistant_skills(service, service.usable_assistant(item_id, user), user)

    return router
