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
    invite_inviter_id       INTEGER,
    invite_inviter_name     TEXT,
    audit_channel_id        INTEGER,
    audit_message_id        INTEGER,
    PRIMARY KEY (user_id, guild_id)
);
CREATE INDEX IF NOT EXISTS idx_members_guild ON members(guild_id);

CREATE TABLE IF NOT EXISTS guild_config (
    guild_id           INTEGER PRIMARY KEY,
    mode               TEXT    NOT NULL DEFAULT 'shadow',
    unverified_role_id INTEGER,
    hold_below_band    TEXT    NOT NULL DEFAULT 'Likely-safe',
    malicious_action   TEXT    NOT NULL DEFAULT 'kick',
    updated_at         TEXT    NOT NULL,
    updated_by         INTEGER
);
"""

# Additive column migrations for existing databases.
# Each entry: (table, column, sql-type).
_MIGRATIONS = [
    ("members", "invite_inviter_id",   "INTEGER"),
    ("members", "invite_inviter_name", "TEXT"),
    ("members", "audit_channel_id",    "INTEGER"),
    ("members", "audit_message_id",    "INTEGER"),
]


@dataclass
class Flag:
    user_id: int
    guild_id: int
    flagged_by: int
    reason: Optional[str]
    created_at: str


@dataclass
class GuildConfig:
    guild_id: int
    mode: str
    unverified_role_id: Optional[int]
    hold_below_band: str
    malicious_action: str
    updated_at: str
    updated_by: Optional[int]


@dataclass
class MemberRecord:
    user_id: int
    guild_id: int
    joined_at: str
    onboarding_completed_at: Optional[str]
    first_message_at: Optional[str]
    invite_code: Optional[str]
    invite_inviter_id: Optional[int] = None
    invite_inviter_name: Optional[str] = None
    audit_channel_id: Optional[int] = None
    audit_message_id: Optional[int] = None


_conn: Optional[aiosqlite.Connection] = None


async def _existing_columns(table: str) -> set[str]:
    assert _conn is not None
    async with _conn.execute(f"PRAGMA table_info({table})") as cursor:
        rows = await cursor.fetchall()
    return {row[1] for row in rows}


async def _run_migrations() -> None:
    """Apply additive column migrations idempotently."""
    assert _conn is not None
    for table, column, spec in _MIGRATIONS:
        cols = await _existing_columns(table)
        if column in cols:
            continue
        await _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")
    await _conn.commit()


async def init() -> None:
    global _conn
    _conn = await aiosqlite.connect(DB_PATH)
    await _conn.executescript(_SCHEMA)
    await _conn.commit()
    await _run_migrations()


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

async def upsert_member_join(
    user_id: int,
    guild_id: int,
    joined_at: str,
    invite_code: Optional[str],
    invite_inviter_id: Optional[int] = None,
    invite_inviter_name: Optional[str] = None,
) -> None:
    """Record a fresh join. On rejoin, resets onboarding, first-message,
    and audit-message references so behavioural tracking and the audit
    embed reflect the new session."""
    assert _conn is not None, "db.init() must be called before use"
    await _conn.execute(
        "INSERT INTO members ("
        "  user_id, guild_id, joined_at, invite_code,"
        "  invite_inviter_id, invite_inviter_name"
        ") VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, guild_id) DO UPDATE SET "
        "  joined_at = excluded.joined_at, "
        "  invite_code = excluded.invite_code, "
        "  invite_inviter_id = excluded.invite_inviter_id, "
        "  invite_inviter_name = excluded.invite_inviter_name, "
        "  onboarding_completed_at = NULL, "
        "  first_message_at = NULL, "
        "  audit_channel_id = NULL, "
        "  audit_message_id = NULL",
        (user_id, guild_id, joined_at, invite_code,
         invite_inviter_id, invite_inviter_name),
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


async def set_audit_message(user_id: int, guild_id: int,
                             channel_id: int, message_id: int) -> None:
    """Remember which mod-channel message holds this member's audit
    embed, so subsequent events can edit it in place."""
    assert _conn is not None, "db.init() must be called before use"
    await _conn.execute(
        "UPDATE members SET audit_channel_id = ?, audit_message_id = ? "
        "WHERE user_id = ? AND guild_id = ?",
        (channel_id, message_id, user_id, guild_id),
    )
    await _conn.commit()


# ---- guild config ----

async def get_guild_config(guild_id: int) -> GuildConfig:
    """Return the guild's enforcement config, inserting defaults if missing."""
    assert _conn is not None, "db.init() must be called before use"
    async with _conn.execute(
        "SELECT guild_id, mode, unverified_role_id, hold_below_band, "
        "malicious_action, updated_at, updated_by "
        "FROM guild_config WHERE guild_id = ?",
        (guild_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is not None:
        return GuildConfig(*row)
    now = _now()
    await _conn.execute(
        "INSERT INTO guild_config (guild_id, updated_at) VALUES (?, ?)",
        (guild_id, now),
    )
    await _conn.commit()
    return GuildConfig(
        guild_id=guild_id, mode="shadow", unverified_role_id=None,
        hold_below_band="Likely-safe", malicious_action="kick",
        updated_at=now, updated_by=None,
    )


async def update_guild_config(
    guild_id: int,
    updated_by: Optional[int],
    mode: Optional[str] = None,
    unverified_role_id: Optional[int] = None,
    hold_below_band: Optional[str] = None,
    malicious_action: Optional[str] = None,
) -> None:
    """Update one or more config fields. Only non-None args are written."""
    assert _conn is not None, "db.init() must be called before use"
    await get_guild_config(guild_id)  # ensure row exists

    updates: list[tuple[str, object]] = []
    if mode is not None:
        updates.append(("mode", mode))
    if unverified_role_id is not None:
        updates.append(("unverified_role_id", unverified_role_id))
    if hold_below_band is not None:
        updates.append(("hold_below_band", hold_below_band))
    if malicious_action is not None:
        updates.append(("malicious_action", malicious_action))
    if not updates:
        return

    updates.append(("updated_at", _now()))
    updates.append(("updated_by", updated_by))

    set_clause = ", ".join(f"{col} = ?" for col, _ in updates)
    values = [v for _, v in updates] + [guild_id]
    await _conn.execute(
        f"UPDATE guild_config SET {set_clause} WHERE guild_id = ?",
        values,
    )
    await _conn.commit()


# ---- member records ----

async def get_member_record(user_id: int, guild_id: int) -> Optional[MemberRecord]:
    assert _conn is not None, "db.init() must be called before use"
    async with _conn.execute(
        "SELECT user_id, guild_id, joined_at, onboarding_completed_at, "
        "first_message_at, invite_code, invite_inviter_id, "
        "invite_inviter_name, audit_channel_id, audit_message_id "
        "FROM members WHERE user_id = ? AND guild_id = ?",
        (user_id, guild_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return MemberRecord(
        user_id=row[0], guild_id=row[1], joined_at=row[2],
        onboarding_completed_at=row[3], first_message_at=row[4],
        invite_code=row[5], invite_inviter_id=row[6],
        invite_inviter_name=row[7], audit_channel_id=row[8],
        audit_message_id=row[9],
    )
