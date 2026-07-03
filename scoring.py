"""Trust scoring for new-joiner audits.

Each signal contributes a small integer weight. Weights sum to a score,
score maps to a band. Bands are informational in shadow mode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import discord

import db

_USERNAME_TRAILING_DIGITS = re.compile(r".*\d{4,}$")


@dataclass
class Signal:
    name: str
    detail: str
    weight: int
    prominent: bool = False


@dataclass
class AuditContext:
    member: discord.Member
    full_user: Optional[discord.User] = None
    active_flag: Optional[db.Flag] = None
    used_invite: Optional[discord.Invite] = None
    total_bot_guilds: int = 1
    member_record: Optional[db.MemberRecord] = None


@dataclass
class Audit:
    signals: list[Signal]
    score: int
    band: str


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _fmt_age(days: int) -> str:
    if days < 60:
        return f"{days}d"
    if days < 730:
        return f"{days // 30}mo"
    return f"{days // 365}y"


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _signal_blocklist(ctx: AuditContext) -> Signal:
    flag = ctx.active_flag
    if flag is None:
        return Signal("Blocklist", "clean", 0)
    date = flag.created_at[:10]
    reason = flag.reason or "no reason given"
    detail = f"flagged {date}: {reason}"
    if len(detail) > 100:
        detail = detail[:97] + "..."
    return Signal("Blocklist", detail, -10, prominent=True)


def _signal_invite(ctx: AuditContext) -> Signal:
    inv = ctx.used_invite
    if inv is None:
        return Signal("Invite", "unknown (vanity URL, cold cache, or retroactive audit)",
                       0, prominent=True)
    creator = inv.inviter
    if creator is None:
        creator_str = "unknown"
    else:
        creator_str = f"{creator.mention} (@{creator.name})"
    return Signal(
        "Invite",
        f"`{inv.code}` — by {creator_str}, {inv.uses} uses",
        0,
        prominent=True,
    )


def _signal_mutual_servers(ctx: AuditContext) -> Signal:
    other_guilds_available = max(0, ctx.total_bot_guilds - 1)
    if other_guilds_available == 0:
        return Signal("Mutual servers", "n/a (bot in 1 guild)", 0)
    mutuals = [g for g in ctx.member.mutual_guilds if g.id != ctx.member.guild.id]
    n = len(mutuals)
    if n == 0:
        weight = -2
    elif n == 1:
        weight = 0
    elif n == 2:
        weight = 1
    else:
        weight = min(n, 3)
    return Signal(
        "Mutual servers",
        f"shares {n} of {other_guilds_available} other bot-guilds",
        weight,
    )


def _signal_account_age(ctx: AuditContext) -> Signal:
    age_days = (datetime.now(timezone.utc) - ctx.member.created_at).days
    if age_days < 30:
        weight = -3
    elif age_days < 90:
        weight = -1
    elif age_days < 365:
        weight = 0
    elif age_days < 730:
        weight = 1
    else:
        weight = 2
    return Signal("Account age", _fmt_age(age_days), weight)


def _signal_avatar(ctx: AuditContext) -> Signal:
    avatar = ctx.member.avatar
    if avatar is None:
        return Signal("Avatar", "default", -2)
    if avatar.is_animated():
        return Signal("Avatar", "animated (Nitro)", 2)
    return Signal("Avatar", "custom", 1)


def _signal_banner(ctx: AuditContext) -> Signal:
    banner = getattr(ctx.full_user, "banner", None) if ctx.full_user else None
    if banner is None:
        return Signal("Banner", "none", 0)
    return Signal("Banner", "custom", 1)


def _signal_avatar_decoration(ctx: AuditContext) -> Signal:
    deco = getattr(ctx.member, "avatar_decoration", None)
    if deco is None and ctx.full_user is not None:
        deco = getattr(ctx.full_user, "avatar_decoration", None)
    if deco is None:
        return Signal("Avatar decoration", "none", 0)
    return Signal("Avatar decoration", "present", 1)


def _signal_server_booster(ctx: AuditContext) -> Signal:
    if ctx.member.premium_since is None:
        return Signal("Server booster", "no", 0)
    return Signal("Server booster", "yes", 3)


def _signal_public_flags(ctx: AuditContext) -> Signal:
    active = ctx.member.public_flags.all()
    if not active:
        return Signal("Public flags", "none", -1)
    names = ", ".join(f.name.replace("_", " ") for f in active[:4])
    weight = min(len(active), 3)
    return Signal("Public flags", names, weight)


def _signal_username_pattern(ctx: AuditContext) -> Signal:
    if _USERNAME_TRAILING_DIGITS.match(ctx.member.name):
        return Signal("Username", f"@{ctx.member.name} — trailing digits", -2)
    return Signal("Username", f"@{ctx.member.name}", 0)


def _signal_onboarding_speed(ctx: AuditContext) -> Signal:
    record = ctx.member_record
    if record is None:
        return Signal("Onboarding speed", "no record (joined before bot deployed)", 0)
    if record.onboarding_completed_at is None:
        return Signal("Onboarding speed", "pending / not completed", 0)
    elapsed = (_parse_ts(record.onboarding_completed_at) - _parse_ts(record.joined_at)).total_seconds()
    if elapsed < 5:
        weight, note = -3, "speedrun"
    elif elapsed < 30:
        weight, note = -1, "fast, no reading"
    elif elapsed < 30 * 60:
        weight, note = 0, "normal"
    else:
        weight, note = 1, "deliberate"
    return Signal("Onboarding speed", f"{fmt_duration(elapsed)} — {note}", weight)


def _signal_first_message_timing(ctx: AuditContext) -> Signal:
    record = ctx.member_record
    if record is None:
        return Signal("First message", "no record (joined before bot deployed)", 0)
    if record.first_message_at is None:
        return Signal("First message", "none yet", 0)
    elapsed = (_parse_ts(record.first_message_at) - _parse_ts(record.joined_at)).total_seconds()
    if elapsed < 30:
        weight, note = -2, "posted immediately"
    else:
        weight, note = 0, "posted later"
    return Signal("First message", f"{fmt_duration(elapsed)} after join — {note}", weight)


_SIGNALS = [
    _signal_blocklist,
    _signal_invite,
    _signal_mutual_servers,
    _signal_account_age,
    _signal_avatar,
    _signal_banner,
    _signal_avatar_decoration,
    _signal_server_booster,
    _signal_public_flags,
    _signal_username_pattern,
    _signal_onboarding_speed,
    _signal_first_message_timing,
]


def _band_for(score: int) -> str:
    if score >= 8:
        return "Trusted"
    if score >= 3:
        return "Likely-safe"
    if score >= -2:
        return "Neutral"
    if score >= -6:
        return "Suspicious"
    return "Malicious"


async def audit(
    member: discord.Member,
    client: discord.Client,
    used_invite: Optional[discord.Invite] = None,
) -> Audit:
    full_user: Optional[discord.User] = None
    try:
        full_user = await client.fetch_user(member.id)
    except discord.HTTPException:
        pass
    active_flag = await db.get_active_flag(member.id)
    member_record = await db.get_member_record(member.id, member.guild.id)
    ctx = AuditContext(
        member=member,
        full_user=full_user,
        active_flag=active_flag,
        used_invite=used_invite,
        total_bot_guilds=len(client.guilds),
        member_record=member_record,
    )
    signals = [fn(ctx) for fn in _SIGNALS]
    score = sum(s.weight for s in signals)
    return Audit(signals=signals, score=score, band=_band_for(score))
