from .security import AppError
from .service import message_view


def message_history(db, conversation_id, limit, *, before=None, after=None, around=None):
    """Read one stable window in the same order as the original messages route."""
    cursors = [cursor for cursor in (before, after, around) if cursor is not None]
    if len(cursors) > 1:
        raise AppError(422, "INVALID_MESSAGE_CURSOR", "每次只能选择一种消息定位方式")
    anchor = None
    if cursors:
        anchor = db.one("SELECT *, rowid AS history_order FROM messages WHERE conversation_id=? AND id=?", (conversation_id, cursors[0]))
        if not anchor:
            raise AppError(404, "MESSAGE_NOT_FOUND", "该消息不可用")

    def window(anchor, *, newer, count):
        comparator, order = (">", "ASC") if newer else ("<", "DESC")
        query = "SELECT *, rowid AS history_order FROM messages WHERE conversation_id=?"
        values = [conversation_id]
        if anchor:
            query += f" AND (created_at, rowid) {comparator} (?, ?)"
            values.extend((anchor["created_at"], anchor["history_order"]))
        rows = db.all(query + f" ORDER BY created_at {order}, rowid {order} LIMIT ?", (*values, count))
        return rows if newer else list(reversed(rows))

    if around:
        left = window(anchor, newer=False, count=limit // 2)
        right = window(anchor, newer=True, count=limit - len(left) - 1)
        rows = [*left, anchor, *right]
    else:
        rows = window(anchor, newer=bool(after), count=limit)
    first, last = (rows[0], rows[-1]) if rows else (anchor, anchor)

    def adjacent(row, *, newer):
        if row is None:
            return None
        comparator = ">" if newer else "<"
        exists = db.one(f"SELECT 1 FROM messages WHERE conversation_id=? AND (created_at, rowid) {comparator} (?, ?) LIMIT 1", (conversation_id, row["created_at"], row["history_order"]))
        return row["id"] if exists else None

    older, newer = adjacent(first, newer=False), adjacent(last, newer=True)
    latest_run = next((row["run_id"] for row in reversed(rows) if row["run_id"]), None)
    if newer or not latest_run:
        last_run = db.one("SELECT run_id FROM messages WHERE conversation_id=? AND run_id IS NOT NULL ORDER BY created_at DESC,rowid DESC LIMIT 1", (conversation_id,))
        latest_run = last_run["run_id"] if last_run else None
    return {"messages": [message_view({key: value for key, value in row.items() if key != "history_order"}) for row in rows],
            "before": older, "after": newer, "latest_run_id": latest_run}
