"""Operations recorded by one deployment, with a public field allowlist."""
from __future__ import annotations

import re
from datetime import datetime

from .security import AppError
from .usage_accounting import usage_entry


SAFE_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,160}")


def _identifier(value):
    return value if isinstance(value, str) and SAFE_ID.fullmatch(value) else None


def activity_page(service, *, user_id: str | None, before: str | None, limit: int, include_reviews: bool = True) -> dict:
    values = []
    predicates = []
    if user_id is not None:
        predicates.append("a.user_id=?")
        values.append(user_id)
    if before:
        try:
            at, key = before.split("|", 1)
            if datetime.fromisoformat(at).tzinfo is None or not SAFE_ID.fullmatch(key):
                raise ValueError
        except ValueError:
            raise AppError(422, "ACTIVITY_CURSOR_INVALID", "活动记录游标无效，请重新查询。") from None
        predicates.append("(a.created_at,a.sort_key)<(?,?)")
        values.extend((at, key))
    where = "WHERE " + " AND ".join(predicates) if predicates else ""
    reviews = """UNION ALL
        SELECT 'review:'||p.id,p.created_at,p.user_id,p.actor_id,'review',p.operation,p.status,p.code,NULL,
               NULL,NULL,NULL,NULL
        FROM prompt_safety_activity p""" if include_reviews else ""
    rows = service.db.all(f"""WITH activity AS (
        SELECT 'run:'||r.id sort_key,r.created_at,r.user_id,NULL actor_id,
               'run' source,r.kind operation,r.status,r.error_code code,NULL tool_name,
               NULL model_id,NULL model_label,r.usage,r.execution_meta
        FROM runs r
        UNION ALL
        SELECT 'model:'||m.id,m.created_at,m.user_id,NULL,'model',m.kind,m.status,NULL,NULL,
               m.model_id,m.model_label,m.usage,NULL
        FROM model_usage_calls m
        UNION ALL
        SELECT 'tool:'||e.run_id||':'||e.id,e.created_at,r.user_id,NULL,'tool',e.type,
               CASE WHEN e.type='tool.started' THEN 'started' WHEN e.type='tool.discarded' THEN 'discarded'
                    WHEN json_extract(e.data,'$.success')=1 THEN 'succeeded'
                    WHEN json_extract(e.data,'$.success')=0 THEN 'failed' ELSE 'unknown' END,
               json_extract(e.data,'$.error_code'),json_extract(e.data,'$.tool_name'),NULL,NULL,NULL,NULL
        FROM events e JOIN runs r ON r.id=e.run_id
        WHERE e.type IN ('tool.started','tool.completed','tool.discarded') AND json_valid(e.data)
        {reviews}
    )
    SELECT a.*,u.display_name,u.username,actor.display_name actor_name
    FROM activity a JOIN users u ON u.id=a.user_id LEFT JOIN users actor ON actor.id=a.actor_id
    {where} ORDER BY a.created_at DESC,a.sort_key DESC LIMIT ?""", (*values, limit + 1))
    items = []
    for row in rows[:limit]:
        item = {field: row[field] for field in ("created_at", "user_id", "display_name", "username", "actor_name", "operation", "status")}
        item.update(id=row["sort_key"], code=_identifier(row["code"]), tool_name=_identifier(row["tool_name"]))
        if row["source"] in {"model", "run"}:
            entry = usage_entry({**row, "id": row["sort_key"], "kind": row["operation"]}, auxiliary=row["source"] == "model")
            item.update(model_label=entry["model_label"], input_tokens=entry["total_input_tokens"],
                        output_tokens=entry["amounts"]["output_tokens"])
        items.append(item)
    tail = rows[limit - 1] if len(rows) > limit else None
    return {"items": items, "next_cursor": f"{tail['created_at']}|{tail['sort_key']}" if tail else None}
