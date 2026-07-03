"""Trust scoring for new-joiner audits.

Each signal contributes a small integer weight. Weights sum to a score,
score maps to a band. Bands are informational in shadow mode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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


@dataclass
class AuditContext:
    member: discord.Member
    full_user: Optional[discord.User] = None
    active_flag: Optional[db.Flag] = None


@dataclass
class Audit:
    signals: list[Signal]
    score: int
    band: str


def _fmt_age(days: int) -> str:
    if days < 60:
        return f"{days}d"
    if days < 730:
        return f"{days // 30}mo"
    return f"{days // 365}y"


def _signal_blocklist(ctx: AuditContext) -> Signal:
    flag = ctx.active_flag
    if flag is None:
        return Signal("Blocklist", "clean", 0)
    date = flag.created_at[:10]
    reason = flag.reason or "no reason given"
    detail = f"flagged {date}: {reason}"
    if len(detail) > 100:
        detail = detail[:97] + "..."
    return Signal("Blocklist", detail, -10)


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


_SIGNALS = [
    _signal_blocklist,
    _signal_account_age,
    _signal_avatar,
    _signal_banner,
    _signal_avatar_decoration,
    _signal_server_booster,
    _signal_public_flags,
    _signal_username_pattern,
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


async def audit(member: discord.Member, client: discord.Client) -> Audit:
    full_user: Optional[discord.User] = None
    try:
        full_user = await client.fetch_user(member.id)
    except discord.HTTPException:
        pass
    active_flag = await db.get_active_flag(member.id)
    ctx = AuditContext(member=member, full_user=full_user, active_flag=active_flag)
    signals = [fn(ctx) for fn in _SIGNALS]
    score = sum(s.weight for s in signals)
    return Audit(signals=signals, score=score, band=_band_for(score))
