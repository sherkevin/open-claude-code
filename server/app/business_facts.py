"""Narrow, deterministic checks of explicitly declared returned-row aggregates.

This is not a general semantic verifier. Unknown prose, units, cross-result
comparisons and undeclared columns are never reported as verified facts.
"""
from __future__ import annotations

from decimal import Decimal, DecimalException, InvalidOperation, localcontext
import re

MAX_SAFE = 9_007_199_254_740_991
NUMBER = r"[-+]?(?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?"
TOTAL = re.compile(r"合计|总计|总数量|总件数|总金额|总额|总数|总量|累计|共计|total", re.I)
UNCERTAIN = re.compile(r"不是|并非|不等于|错误|有误|误写|不应|假设|假如|如果|若|可能|示例|上次|此前|之前|历史|过去|预测|预计|未来|(?:上|下|去|昨|前|后)[个一二三\d]*[年月日周季]|[‘’“”\"?？]|\b(?:not|previous|last|example)\b", re.I)


def decimal_value(value):
    if type(value) not in (int, float) or type(value) is int and abs(value) > MAX_SAFE:
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def verify_aggregates(payload, columns, rows):
    """Only a tool's explicit additive contract permits host arithmetic."""
    declarations = payload.get("aggregates")
    if not isinstance(declarations, list) or len(declarations) > 24:
        return []
    verified, seen = [], set()
    for item in declarations:
        if not isinstance(item, dict) or item.get("operation") != "sum" or item.get("scope") != "returned_rows":
            continue
        key = item.get("column")
        if not isinstance(key, str) or key in seen:
            continue
        column = next((col for col in columns if col["key"] == key), None)
        label, unit = item.get("label"), item.get("unit")
        if (not column or not isinstance(label, str) or not 1 <= len(label) <= 100
                or not isinstance(unit, str) or not 1 <= len(unit) <= 40 or column.get("unit") != unit):
            continue
        claimed = decimal_value(item.get("value"))
        values = [decimal_value(row.get(key)) for row in rows]
        if claimed is None or any(value is None for value in values):
            continue
        # Every accepted input is a safe finite JSON number. Use enough decimal
        # precision for exact sum of bounded rows, including subnormal floats.
        with localcontext() as context:
            context.prec = 700
            actual = sum(values, Decimal(0))
        if actual != claimed:
            continue
        seen.add(key)
        aliases = item.get("claim_labels", [])
        aliases = aliases if (isinstance(aliases, list) and len(aliases) <= 8
                              and all(isinstance(label, str) and 1 <= len(label) <= 80 for label in aliases)) else []
        verified.append({"column": key, "label": label, "unit": unit, "value": str(actual),
                         "operation": "sum", "scope": "returned_rows", "verification": "host_decimal_sum",
                         "column_label": column["label"], "claim_labels": aliases})
    return verified


def _units(unit):
    # Exact currency alias, never an exchange-rate or magnitude conversion.
    return ("CNY", "元", "人民币") if unit == "CNY" else (unit,)


def _number(text):
    text = text.strip().replace("*", "").replace("`", "")
    if len(text) > 100:
        return None
    match = re.fullmatch(rf"({NUMBER})\s*([万亿]?)", text)
    if not match:
        return None
    try:
        with localcontext() as context:
            context.prec = 120
            return Decimal(match[1].replace(",", "").replace("，", "")) * {"": 1, "万": 10000, "亿": 100000000}[match[2]]
    except DecimalException:
        return None


def _header_matches(header, fact):
    for unit in _units(fact["unit"]):
        header = re.sub(rf"\s*[（(]{re.escape(unit)}[）)]\s*$", "", header)
    return header.strip() in _labels(fact)


def _labels(fact):
    return {fact["column_label"], fact["column"], fact["label"], *fact.get("claim_labels", [])}


def _table_is_same_returned_rows(result, header, body, fact):
    """Prove row membership from literal identifiers and returned scalar values."""
    from collections import Counter
    if not body or len(body) != len(result["rows"]):
        return False
    keys = []
    for cell in header:
        candidates = []
        for column in result["columns"]:
            declaration = next((value for value in result.get("host_verified_aggregates", [])
                                if value["column"] == column["key"]), None)
            if declaration and _header_matches(cell, declaration):
                candidates.append(column["key"])
            else:
                name = cell
                for unit in _units(column.get("unit", "")):
                    if unit:
                        name = re.sub(rf"\s*[（(]{re.escape(unit)}[）)]\s*$", "", name)
                if name.strip() in {column["key"], column["label"]}:
                    candidates.append(column["key"])
        if len(set(candidates)) != 1:
            return False
        keys.append(candidates[0])
    if fact["column"] not in keys or len(set(keys)) != len(keys):
        return False
    # A table of anonymous numbers cannot prove membership, even when counts
    # happen to match. Require unique literal string identifiers in the subset.
    identifiers = [key for key in keys if all(type(row.get(key)) is str for row in result["rows"])]
    if not identifiers or len({tuple(row[key] for key in identifiers) for row in result["rows"]}) != len(body):
        return False
    actual, expected = [], []
    for row in result["rows"]:
        expected.append(tuple(decimal_value(row[key]) if type(row[key]) in (int, float) else row[key] for key in keys))
    for cells in body:
        if len(cells) != len(header):
            return False
        values = []
        for key, cell in zip(keys, cells):
            column = next(value for value in result["columns"] if value["key"] == key)
            if key in identifiers:
                values.append(cell)
            else:
                numeric = cell
                for unit in _units(column.get("unit", "")):
                    if unit:
                        numeric = numeric.removesuffix(unit).strip()
                value = _number(numeric)
                if value is None:
                    return False
                values.append(value)
        actual.append(tuple(values))
    return Counter(actual) == Counter(expected)


def check_answer(text, results):
    """Hard-fail only provably same-row totals; all other prose is unverified."""
    base = {"status": "not_checked", "coverage": "same_returned_rows_aggregate_claims_only", "conflicts": [], "checked_claims": 0}
    if len(results) != 1:
        return base
    result = results[0]
    facts = result.get("host_verified_aggregates", [])
    if not facts or UNCERTAIN.search(text) or "不成立" in text:
        return base
    claims, header, body = [], None, []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or line.lstrip().startswith(">"):
            continue
        plain = line.replace("*", "").replace("`", "")
        if "|" not in plain:
            header, body = None, []
            continue
        cells = [cell.strip() for cell in plain.strip().strip("|").split("|")]
        if all(re.fullmatch(r"[-: ]*", cell) for cell in cells):
            continue
        total_row = cells[0] in {"合计", "总计", "累计", "共计", "total", "Total", "Grand Total", *(fact["label"] for fact in facts)}
        if total_row:
            if header and len(cells) == len(header):
                for index, cell in enumerate(cells):
                    candidates = [fact for fact in facts if _header_matches(header[index], fact)]
                    if len(candidates) != 1 or not _table_is_same_returned_rows(result, header, body, candidates[0]):
                        continue
                    numeric = cell
                    for unit in _units(candidates[0]["unit"]):
                        numeric = numeric.removesuffix(unit).strip()
                    value = _number(numeric)
                    if value is not None:
                        claims.append((candidates[0], value))
            continue
        if header is None:
            header = cells
        else:
            body.append(cells)
    # The only prose form is an entire affirmative paragraph explicitly naming
    # all returned rows. No scanning for later numbers or implied scope.
    for paragraph in re.split(r"\n\s*\n", text):
        if "|" in paragraph or "\n" in paragraph.strip() or paragraph.lstrip().startswith((">", "```", "~~~")):
            continue
        plain = paragraph.replace("*", "").replace("`", "").strip()
        plain = re.sub(r"\[\[source:[A-Za-z0-9_-]+\]\]|\[[^\]\n]+\]\(/api/v1/sources/[A-Za-z0-9_-]+\)", "", plain).strip()
        scope = re.match(r"本次(?:全部)?返回(?:的全部|的)?(?:(\d{1,3})\s*)?行(?:的)?[，,:： ]*", plain)
        if not scope or scope[1] and int(scope[1]) != result["returned_rows"]:
            continue
        for fact in facts:
            if sum(bool(set(_units(other["unit"])) & set(_units(fact["unit"]))) for other in facts) != 1:
                continue
            labels = "|".join(re.escape(label) for label in sorted(_labels(fact), key=len, reverse=True))
            units = "|".join(re.escape(unit) for unit in _units(fact["unit"]))
            match = re.fullmatch(rf"(?:(?:{labels}))?(?:合计|总计)\s*(?:为|是|:|：|=)?\s*({NUMBER})\s*([万亿]?)\s*(?:{units})[。.!]?", plain[scope.end():])
            if match:
                value = _number(match[1] + match[2])
                if value is not None:
                    claims.append((fact, value))
    conflicts = [{"column": fact["column"], "label": fact["label"], "unit": fact["unit"],
                  "scope": fact["scope"], "expected": fact["value"], "claimed": str(value)}
                 for fact, value in claims if value != Decimal(fact["value"])]
    return {**base, "status": "conflict" if conflicts else "no_detected_conflict" if claims else "not_checked",
            "conflicts": conflicts, "checked_claims": len(claims)}


def conflict_answer(original, result):
    facts = "；".join(f"{fact['label']} {fact['value']} {fact['unit']}" for fact in result["host_verified_aggregates"])
    citations = " ".join(f"[[source:{source}]]" for source in result.get("source_ids", []))
    # Keep the entire original visible and in the audit event. This is a failed
    # answer with a correction, never a silent replacement marked successful.
    quoted = "\n".join("> " + line for line in original.splitlines())
    return ("本轮模型回答未通过业务事实校验，不能作为已核验结论。\n\n"
            f"宿主核对的本次返回行合计：{facts}。这只覆盖本次返回的 {result['returned_rows']} 行，不推断全量。{citations}\n\n"
            "以下保留原始模型回答（其中冲突数字无效，仅供复核）：\n\n" + quoted)
