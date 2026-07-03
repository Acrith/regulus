"""SQLite persistence for audits and blocklist flags."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

_BOT_DIR = Path(__file__).parent
DB_PATH = Path(os.getenv("DB_PATH", str(_BOT_DIR / "regulus.db")))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS audits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    guild_id     INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    score        INTEGER NOT NULL,
    band         TEXT NOT NULL,
    signals_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audits_user    ON audits(user_id);
CREATE INDEX IF NOT EXISTS idx_audits_guild   ON audits(guild_id);
CREATE INDEX IF NOT EXISTS idx_audits_created ON audits(created_at);

CREATE TABLE IF NOT EXISTS flags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    flagged_by  INTEGER NOT NULL,
    reason      TEXT,
    created_at  TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_flags_user_active ON flags(user_id, active);
"""


@dataclass
class Flag:
    user_id: int
    guild_id: int
    flagged_by: int
    reason: Optional[str]
    created_at: str


_conn: Optional[aiosqlite.Connection] = None


async def init() -> None:
    global _conn
    _conn = await aiosqlite.connect(DB_PATH)
    await _conn.executescript(_SCHEMA)
    await _conn.commit()


async def close() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def get_active_flag(user_id: int) -> Optional[Flag]:
    assert _conn is not None, "db.init() must be called before use"
    async with _conn.execute(
        "SELECT user_id, guild_id, flagged_by, reason, created_at "
        "FROM flags WHERE user_id = ? AND active = 1 "
        "ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return Flag(user_id=row[0], guild_id=row[1], flagged_by=row[2],
                reason=row[3], created_at=row[4])


async def add_flag(user_id: int, guild_id: int, flagged_by: int,
                    reason: Optional[str]) -> None:
    assert _conn is not None, "db.init() must be called before use"
    await _conn.execute(
        "INSERT INTO flags (user_id, guild_id, flagged_by, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, guild_id, flagged_by, reason, _now()),
    )
    await _conn.commit()


async def deactivate_flags(user_id: int) -> int:
    assert _conn is not None, "db.init() must be called before use"
    cursor = await _conn.execute(
        "UPDATE flags SET active = 0 WHERE user_id = ? AND active = 1",
        (user_id,),
    )
    await _conn.commit()
    return cursor.rowcount


async def record_audit(user_id: int, guild_id: int, kind: str,
                        score: int, band: str, signals_json: str) -> None:
    assert _conn is not None, "db.init() must be called before use"
    await _conn.execute(
        "INSERT INTO audits "
        "(user_id, guild_id, kind, score, band, signals_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, guild_id, kind, score, band, signals_json, _now()),
    )
    await _conn.commit()
