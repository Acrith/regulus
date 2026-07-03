"""SQLite persistence for audits, blocklist flags, and per-member behavioural data."""

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

CREATE TABLE IF NOT EXISTS members (
    user_id                 INTEGER NOT NULL,
    guild_id                INTEGER NOT NULL,
    joined_at               TEXT NOT NULL,
    onboarding_completed_at TEXT,
    first_message_at        TEXT,
    invite_code             TEXT,
    PRIMARY KEY (user_id, guild_id)
);
CREATE INDEX IF NOT EXISTS idx_members_guild ON members(guild_id);
"""


@dataclass
class Flag:
    user_id: int
    guild_id: int
    flagged_by: int
    reason: Optional[str]
    created_at: str


@dataclass
class MemberRecord:
    user_id: int
    guild_id: int
    joined_at: str
    onboarding_completed_at: Optional[str]
    first_message_at: Optional[str]
    invite_code: Optional[str]


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


# ---- flags ----

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


# ---- audits history ----

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


# ---- member records ----

async def upsert_member_join(user_id: int, guild_id: int, joined_at: str,
                              invite_code: Optional[str]) -> None:
    """Record a fresh join. On rejoin, resets onboarding and first-message
    timestamps so behavioural tracking reflects the new session."""
    assert _conn is not None, "db.init() must be called before use"
    await _conn.execute(
        "INSERT INTO members (user_id, guild_id, joined_at, invite_code) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, guild_id) DO UPDATE SET "
        "  joined_at = excluded.joined_at, "
        "  invite_code = excluded.invite_code, "
        "  onboarding_completed_at = NULL, "
        "  first_message_at = NULL",
        (user_id, guild_id, joined_at, invite_code),
    )
    await _conn.commit()


async def set_onboarding_completed(user_id: int, guild_id: int,
                                    timestamp: str) -> bool:
    """Set the completion timestamp if not already set. Returns True if set."""
    assert _conn is not None, "db.init() must be called before use"
    cursor = await _conn.execute(
        "UPDATE members SET onboarding_completed_at = ? "
        "WHERE user_id = ? AND guild_id = ? "
        "AND onboarding_completed_at IS NULL",
        (timestamp, user_id, guild_id),
    )
    await _conn.commit()
    return cursor.rowcount > 0


async def set_first_message(user_id: int, guild_id: int,
                             timestamp: str) -> bool:
    """Set the first-message timestamp if not already set. Returns True if set."""
    assert _conn is not None, "db.init() must be called before use"
    cursor = await _conn.execute(
        "UPDATE members SET first_message_at = ? "
        "WHERE user_id = ? AND guild_id = ? "
        "AND first_message_at IS NULL",
        (timestamp, user_id, guild_id),
    )
    await _conn.commit()
    return cursor.rowcount > 0


async def get_member_record(user_id: int, guild_id: int) -> Optional[MemberRecord]:
    assert _conn is not None, "db.init() must be called before use"
    async with _conn.execute(
        "SELECT user_id, guild_id, joined_at, onboarding_completed_at, "
        "first_message_at, invite_code "
        "FROM members WHERE user_id = ? AND guild_id = ?",
        (user_id, guild_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return MemberRecord(
        user_id=row[0], guild_id=row[1], joined_at=row[2],
        onboarding_completed_at=row[3], first_message_at=row[4],
        invite_code=row[5],
    )
