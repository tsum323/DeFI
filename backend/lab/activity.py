"""Activity log (UNISWAP AI LAB): what the system actually did, in plain language.

Every step of the daily cycle writes one line with a status: ok | warn | error | info.
A failed or skipped step is written as such — never as a success.
"""

from __future__ import annotations

import json
import time

from sqlalchemy import insert, select

from backend.database import schema as S


def log_activity(engine, kind: str, status: str, message: str, details: dict | None = None,
                 now: int | None = None) -> None:
    assert status in ("ok", "warn", "error", "info"), status
    with engine.begin() as conn:
        conn.execute(insert(S.activity).values(
            created_at=now or int(time.time()), kind=kind, status=status, message=message,
            details_json=json.dumps(details, default=str) if details else None))


def recent_activity(engine, limit: int = 200) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(select(S.activity).order_by(S.activity.c.id.desc()).limit(limit)).all()
    return [{"id": r.id, "at": r.created_at, "kind": r.kind, "status": r.status, "message": r.message,
             "details": json.loads(r.details_json) if r.details_json else None} for r in rows]
