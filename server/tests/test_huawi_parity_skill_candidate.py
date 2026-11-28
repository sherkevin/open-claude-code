"""Acceptance checks for the bounded read-only Huawi parity Skill candidate.

The tests only read the candidate SKILL.md, the existing synthetic industrial
fixture, and the local FastAPI app.  They do not call a model, a customer MCP,
production, or any network endpoint.  The synthetic query plans below exist to
encode the candidate's rules as executable positive and negative examples; they
are not tool transcripts.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

from app.security import AppError
from app.skills import SkillImport, import_skill
from test_api import client, configured  # noqa: F401  (pytest fixtures)


ROOT = Path(__file__).resolve().parents[2]
CANDIDATE = ROOT / "examples" / "huawi-parity-readonly" / "SKILL.md"
FIXTURE_ROOT = ROOT / "fixtures" / "synthetic-industrial-v1"

FULL_COPY_CLAIM = (
    "不是客户原始 SKILL.md、插件清单、提示词、运行配置、业务数据或答案的完整副本"
)

REQUIRED_MARKERS = (
    ("source_boundary", r"不是[^\n]{0,80}完整副本"),
    ("semantic_decomposition", r"对象、范围、指标、单位"),
    ("describe_before_query", r"先 describe 再 query"),
    ("no_field_guessing", r"不得猜测字段名"),
    ("finite_discovery", r"有限发现"),
    ("explicit_count_alias", r"COUNT\(DISTINCT batch_id\) AS batch_count"),
    ("auxiliary_statistics", r"辅助统计"),
    ("no_substitution", r"不替换"),
    ("zero_vs_empty", r"零、空、未查询、失败与无权限"),
    ("partial_pages", r"has_more=false"),
    ("no_unsafe_action", r"不执行不安全动作"),
    ("declarative_only", r"不注册、不授予、不扩大任何可执行工具或数据权限"),
)

FORBIDDEN_PATTERNS = (
    ("unsafe_claim", r"自动执行|无需审批|导出全部|直接执行|已经执行"),
    (
        "customer_identifier",
        r"CAP-\d{3}|CUSTOMER-INTERNAL-|tenant-acme-|"
        r"(?:customer|tenant|客户|租户)[-_ ]?(?:internal[-_ ]?)?(?:id|标识|编号)\s*[:=]",
    ),
    (
        "secret_material",
        r"sk-[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9._-]{16,}|"
        r"password\s*[:=]|token\s*[:=]\s*\S+",
    ),
)


def candidate_text() -> str:
    return CANDIDATE.read_text(encoding="utf-8")


def fixture_fields() -> set[str]:
    with (FIXTURE_ROOT / "tables" / "quality-batches.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        return set(next(csv.reader(handle)))


def audit_candidate(text: str) -> list[str]:
    """Return acceptance violations for a candidate SKILL.md text."""
    violations: list[str] = []
    try:
        parsed = import_skill(SkillImport(markdown=text))
    except Exception as exc:  # noqa: BLE001 - report the exact import failure to the lead
        return [f"import_error:{type(exc).__name__}"]
    if parsed["name"] != "huawi-parity-readonly":
        violations.append("wrong_name")
    if parsed["required_tools"]:
        violations.append("declared_tools")
    if not parsed["instructions"].startswith("# "):
        violations.append("instructions_shape")
    for code, pattern in REQUIRED_MARKERS:
        if not re.search(pattern, text):
            violations.append(code)
    for code, pattern in FORBIDDEN_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            violations.append(code)
    return violations


def _is_count(expression: str) -> bool:
    return "COUNT(" in expression.upper()


def _has_explicit_alias(expression: str) -> bool:
    return bool(re.search(r"\bAS\s+[A-Za-z_][A-Za-z0-9_]*\s*$", expression.strip(), re.I))


def validate_query_plan(plan: dict) -> list[str]:
    """A small synthetic checker for the plan rules stated in the candidate."""
    errors: list[str] = []
    fields = set(plan["described_fields"])
    objects = set(plan["described_objects"])
    associations = set(plan["described_associations"])
    primary = plan["primary_object"]

    if primary not in objects:
        if not plan.get("explain_missing_relationship"):
            errors.append("unverified_object")
        if plan.get("substituted_object"):
            errors.append("substitution")
    requested = plan.get("requested_association")
    if requested and requested not in associations:
        if not plan.get("explain_missing_relationship"):
            errors.append("unsupported_relationship")
        if plan.get("substituted_field") or plan.get("substituted_object"):
            errors.append("substitution")

    group_by = list(plan.get("group_by", []))
    if any(field not in fields for field in group_by):
        errors.append("unverified_field")

    plain_selects: list[str] = []
    has_count = False
    for expression in plan.get("select", []):
        stripped = expression.strip()
        if stripped == "*":
            errors.append("wildcard_not_count")
            continue
        if _is_count(stripped):
            has_count = True
            if not _has_explicit_alias(stripped):
                errors.append("implicit_count_alias")
            distinct = re.search(
                r"COUNT\(\s*DISTINCT\s+([A-Za-z_][A-Za-z0-9_]*)\s*\)",
                stripped,
                re.I,
            )
            if "DISTINCT" in stripped.upper() and not distinct:
                errors.append("invalid_count_expression")
            if distinct and distinct.group(1) not in fields:
                errors.append("unverified_field")
            continue
        plain_selects.append(stripped)
        if stripped not in fields:
            errors.append("unverified_field")

    for clause in ("filters", "sort"):
        for item in plan.get(clause, []):
            field = item.get("field") if isinstance(item, dict) else item
            if field not in fields:
                errors.append("unverified_field")

    if group_by and sorted(plain_selects) != sorted(group_by):
        errors.append("group_by_mismatch")
    if not group_by and plain_selects and has_count:
        errors.append("group_by_mismatch")
    if group_by and has_count and not plan.get("grouped_aggregate_proof"):
        errors.append("grouped_aggregate_unproven")

    auxiliary = plan.get("auxiliary", [])
    if len(auxiliary) > 3:
        errors.append("too_many_auxiliary")
    for item in auxiliary:
        if not all(item.get(key) for key in ("object", "scope", "alias", "unit")):
            errors.append("auxiliary_missing_label")
        if any(field not in fields for field in item.get("fields", [])):
            errors.append("unverified_field")
        if not _has_explicit_alias(item.get("expression", "")):
            errors.append("implicit_count_alias")
        if item.get("claims_primary_relationship"):
            errors.append("auxiliary_claimed_as_primary")

    if not plan.get("select") and not auxiliary:
        errors.append("empty_plan")
    return sorted(set(errors))


def validate_answer(claim: dict, result: dict) -> list[str]:
    """Check zero/empty/not-queried/failed/denied and page completeness."""
    errors: list[str] = []
    kind = result.get("kind")
    count = claim.get("count")
    if result.get("queried") is False and count == 0:
        errors.append("unqueried_reported_as_zero")
    if kind == "failed" and count == 0:
        errors.append("failure_reported_as_zero")
    if kind == "denied" and count == 0:
        errors.append("denied_reported_as_zero")
    if kind == "empty_page" and count == 0:
        errors.append("empty_page_reported_as_zero")
    if result.get("has_more") and claim.get("complete"):
        errors.append("partial_page_claimed_complete")
    if kind == "verified_zero" and count != 0:
        errors.append("verified_zero_mismatch")
    if kind == "verified_rows" and not result.get("rows"):
        errors.append("verified_rows_mismatch")
    return sorted(set(errors))


UNSAFE_ACTIONS = {
    "write",
    "update",
    "delete",
    "export",
    "send_message",
    "external_call",
    "create_ticket",
    "execute_call",
}


def validate_actions(actions: list[str]) -> list[str]:
    return sorted({f"unsafe_action:{item}" for item in actions if item in UNSAFE_ACTIONS})


def base_plan(fields: set[str]) -> dict:
    return {
        "primary_object": "synthetic_quality_batches",
        "described_objects": ["synthetic_quality_batches"],
        "described_fields": fields,
        "described_associations": [],
        "select": ["COUNT(*) AS record_count"],
        "group_by": [],
        "filters": [
            {
                "field": "production_date",
                "op": "between",
                "value": ["2026-11-01", "2026-11-04"],
            }
        ],
        "sort": [{"field": "production_date"}],
        "substituted_object": None,
        "substituted_field": None,
        "explain_missing_relationship": False,
        "grouped_aggregate_proof": False,
        "auxiliary": [],
    }


def test_candidate_parses_through_the_existing_skill_import_format():
    text = candidate_text()
    parsed = import_skill(SkillImport(markdown=text))
    assert parsed["name"] == "huawi-parity-readonly"
    assert parsed["description"].startswith("受限的只读业务问答规则候选")
    assert parsed["instructions"].startswith("# ")
    assert "---" not in parsed["instructions"].splitlines()[0]
    assert parsed["required_tools"] == []
    assert len(parsed["instructions"]) <= 50000


def test_candidate_content_passes_the_bounded_rule_audit():
    assert audit_candidate(candidate_text()) == []


def test_audit_rejects_synthetic_negative_candidates():
    text = candidate_text()
    mutated = text.replace(FULL_COPY_CLAIM, "是客户原始 Skill 的完整副本", 1)
    assert mutated != text
    assert "source_boundary" in audit_candidate(mutated)

    mutated = text.replace(
        "COUNT(DISTINCT batch_id) AS batch_count", "COUNT(DISTINCT batch_id)", 1
    )
    assert mutated != text
    assert "explicit_count_alias" in audit_candidate(mutated)

    mutated = text.replace("不得猜测字段名", "可以猜测字段名", 1)
    assert mutated != text
    assert "no_field_guessing" in audit_candidate(mutated)

    assert "unsafe_claim" in audit_candidate(
        text + "\n可以自动执行 call 并导出全部客户数据。"
    )
    assert "customer_identifier" in audit_candidate(
        text + "\n参考 tenant-acme-0001 的配置。"
    )


def test_synthetic_verified_count_plan_is_accepted():
    fields = fixture_fields()
    assert validate_query_plan(base_plan(fields)) == []


def test_synthetic_count_distinct_and_auxiliary_plans_are_accepted():
    fields = fixture_fields()
    distinct = base_plan(fields)
    distinct["select"] = [
        "COUNT(DISTINCT batch_id) AS batch_count",
    ]
    assert validate_query_plan(distinct) == []

    missing_relationship = base_plan(fields)
    missing_relationship.update(
        {
            "primary_object": "synthetic_equipment_failure",
            "requested_association": "batch_id -> equipment_failure",
            "explain_missing_relationship": True,
            "select": [],
            "auxiliary": [
                {
                    "object": "synthetic_quality_batches",
                    "scope": "production_date 2026-11-01..2026-11-04",
                    "fields": ["batch_id"],
                    "expression": "COUNT(DISTINCT batch_id) AS batch_count",
                    "alias": "batch_count",
                    "unit": "批",
                    "claims_primary_relationship": False,
                }
            ],
        }
    )
    assert validate_query_plan(missing_relationship) == []


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"select": ["yield_rate"]}, "unverified_field"),
        ({"select": ["*"]}, "wildcard_not_count"),
        ({"select": ["COUNT(*)"]}, "implicit_count_alias"),
        (
            {"select": ["line", "COUNT(*) AS record_count"]},
            "group_by_mismatch",
        ),
        ({"filters": [{"field": "status"}]}, "unverified_field"),
        (
            {
                "primary_object": "synthetic_missing_dataset",
                "explain_missing_relationship": False,
            },
            "unverified_object",
        ),
        (
            {
                "primary_object": "synthetic_missing_dataset",
                "explain_missing_relationship": True,
                "substituted_object": "synthetic_quality_batches",
            },
            "substitution",
        ),
        (
            {
                "select": ["line", "COUNT(*) AS record_count"],
                "group_by": ["line"],
            },
            "grouped_aggregate_unproven",
        ),
        (
            {
                "select": [],
                "auxiliary": [
                    {
                        "object": "synthetic_quality_batches",
                        "scope": "all",
                        "fields": ["batch_id"],
                        "expression": "COUNT(*) AS record_count",
                        "alias": "record_count",
                        "unit": "条",
                        "claims_primary_relationship": True,
                    }
                ],
            },
            "auxiliary_claimed_as_primary",
        ),
    ],
)
def test_synthetic_negative_plans_are_rejected(overrides, expected):
    fields = fixture_fields()
    plan = {**base_plan(fields), **overrides}
    assert expected in validate_query_plan(plan)


@pytest.mark.parametrize(
    ("claim", "result", "expected"),
    [
        ({"count": 0}, {"queried": False, "kind": "not_queried"}, "unqueried_reported_as_zero"),
        ({"count": 0}, {"kind": "failed"}, "failure_reported_as_zero"),
        ({"count": 0}, {"kind": "denied"}, "denied_reported_as_zero"),
        (
            {"count": 0, "complete": False},
            {"kind": "empty_page", "has_more": True},
            "empty_page_reported_as_zero",
        ),
        (
            {"count": 4, "complete": True},
            {"kind": "verified_rows", "rows": [{"batch_id": "QA-SYN-1101"}], "has_more": True},
            "partial_page_claimed_complete",
        ),
    ],
)
def test_synthetic_negative_answer_states_are_rejected(claim, result, expected):
    assert expected in validate_answer(claim, result)


@pytest.mark.parametrize(
    ("claim", "result"),
    [
        (
            {"count": 0, "complete": True},
            {"kind": "verified_zero", "rows": [], "has_more": False},
        ),
        (
            {"count": 1, "complete": True},
            {"kind": "verified_rows", "rows": [{"record_count": 1}], "has_more": False},
        ),
    ],
)
def test_synthetic_positive_answer_states_are_accepted(claim, result):
    assert validate_answer(claim, result) == []


def test_unsafe_actions_are_never_accepted():
    assert validate_actions(["describe", "query"]) == []
    assert validate_actions(["export", "write", "send_message", "execute_call"]) == [
        "unsafe_action:execute_call",
        "unsafe_action:export",
        "unsafe_action:send_message",
        "unsafe_action:write",
    ]


def test_candidate_imports_through_api_as_a_versioned_declarative_skill(client):
    response = client.post(
        "/api/v1/skills/import",
        json={"markdown": candidate_text(), "scope": "private"},
    )
    assert response.status_code == 201, response.text
    skill = response.json()
    assert skill["name"] == "huawi-parity-readonly"
    assert skill["required_tools"] == []
    assert skill["revision"] == 1
    assert "不是" in skill["instructions"] and "完整副本" in skill["instructions"]

    updated = client.patch(
        f"/api/v1/skills/{skill['id']}",
        json={"instructions": skill["instructions"] + "\n补充：保持只读。"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == 2
    versions = client.get(f"/api/v1/skills/{skill['id']}/versions").json()
    assert [item["revision"] for item in versions] == [2, 1]


def test_imported_candidate_grants_no_executable_tools(client):
    response = client.post(
        "/api/v1/skills/import", json={"markdown": candidate_text()}
    )
    assert response.status_code == 201, response.text
    skill = response.json()
    assistant_response = client.post(
        "/api/v1/assistants",
        json={"name": "只读候选助手", "skill_ids": [skill["id"]]},
    )
    assert assistant_response.status_code == 201, assistant_response.text
    assistant = assistant_response.json()
    conversation_response = client.post(
        "/api/v1/conversations", json={"assistant_id": assistant["id"]}
    )
    assert conversation_response.status_code == 201, conversation_response.text
    conversation = conversation_response.json()
    with pytest.raises(AppError) as denied:
        client.app.state.service.authorize_tool(
            conversation["id"], "Bash", {"command": "echo blocked"}
        )
    assert denied.value.code == "TOOL_DENIED"


def test_declared_tool_metadata_never_authorizes_execution(client):
    # Import accepts declarative tool names, but they remain metadata and do
    # not enlarge the assistant's execution allowlist.
    text = candidate_text().replace(
        "description:", "allowed-tools: [Bash, call]\ndescription:", 1
    )
    response = client.post("/api/v1/skills/import", json={"markdown": text})
    assert response.status_code == 201, response.text
    skill = response.json()
    assert skill["required_tools"] == ["Bash", "call"]
    assistant_response = client.post(
        "/api/v1/assistants",
        json={"name": "声明工具元数据助手", "skill_ids": [skill["id"]]},
    )
    assert assistant_response.status_code == 201, assistant_response.text
    assistant = assistant_response.json()
    conversation_response = client.post(
        "/api/v1/conversations", json={"assistant_id": assistant["id"]}
    )
    assert conversation_response.status_code == 201, conversation_response.text
    conversation = conversation_response.json()
    with pytest.raises(AppError) as denied:
        client.app.state.service.authorize_tool(
            conversation["id"], "Bash", {"command": "echo blocked"}
        )
    assert denied.value.code == "TOOL_DENIED"
