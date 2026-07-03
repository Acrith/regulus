"""Trust scoring for new-joiner audits.

Each signal contributes a small integer weight. Weights sum to a score,
score maps to a band. Bands are informational in shadow mode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Union

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
    user: discord.User  # always present (Member is a User)
    guild_id: int
    member: Optional[discord.Member] = None  # None if user is not a current member of guild_id
    full_user: Optional[discord.User] = None
    active_flag: Optional[db.Flag] = None
    used_invite: Optional[discord.Invite] = None
    total_bot_guilds: int = 1
    member_record: Optional[db.MemberRecord] = None
    client: Optional[discord.Client] = None


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
    guild = ctx.client.get_guild(flag.guild_id) if ctx.client else None
    guild_str = f"**{guild.name}**" if guild else f"guild `{flag.guild_id}`"
    detail = (
        f"by <@{flag.flagged_by}> in {guild_str} on {date}\n"
        f"Reason: {reason}"
    )
    if len(detail) > 400:
        detail = detail[:397] + "..."
    return Signal("Blocklist", detail, -10, prominent=True)


def _signal_invite(ctx: AuditContext) -> Signal:
    inv = ctx.used_invite
    if inv is not None:
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

    # Fallback: reconstruct from the member record persisted at join.
    record = ctx.member_record
    if record and record.invite_code:
        if record.invite_inviter_id:
            creator_str = (
                f"<@{record.invite_inviter_id}> "
                f"(@{record.invite_inviter_name or 'unknown'})"
            )
        else:
            creator_str = "unknown"
        return Signal(
            "Invite",
            f"`{record.invite_code}` — by {creator_str} (persisted at join)",
            0,
            prominent=True,
        )

    return Signal("Invite", "unknown (vanity URL, cold cache, or retroactive audit)",
                   0, prominent=True)


def _signal_mutual_servers(ctx: AuditContext) -> Signal:
    other_guilds_available = max(0, ctx.total_bot_guilds - 1)
    if other_guilds_available == 0:
        return Signal("Mutual servers", "n/a (bot in 1 guild)", 0)
    mutuals = [g for g in ctx.user.mutual_guilds if g.id != ctx.guild_id]
    n = len(mutuals)
    return Signal(
        "Mutual servers",
        f"shares {n} of {other_guilds_available} other bot-guilds (informational)",
        0,
    )


def _signal_account_age(ctx: AuditContext) -> Signal:
    age_days = (datetime.now(timezone.utc) - ctx.user.created_at).days
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
    avatar = ctx.user.avatar
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
    deco = getattr(ctx.user, "avatar_decoration", None)
    if deco is None and ctx.full_user is not None:
        deco = getattr(ctx.full_user, "avatar_decoration", None)
    if deco is None:
        return Signal("Avatar decoration", "none", 0)
    return Signal("Avatar decoration", "present", 1)


def _signal_server_booster(ctx: AuditContext) -> Signal:
    if ctx.member is None:
        return Signal("Server booster", "n/a (not in current guild)", 0)
    if ctx.member.premium_since is None:
        return Signal("Server booster", "no", 0)
    return Signal("Server booster", "yes", 3)


def _signal_public_flags(ctx: AuditContext) -> Signal:
    active = ctx.user.public_flags.all()
    if not active:
        return Signal("Public flags", "none", -1)
    names = ", ".join(f.name.replace("_", " ") for f in active[:4])
    weight = min(len(active), 3)
    return Signal("Public flags", names, weight)


def _signal_username_pattern(ctx: AuditContext) -> Signal:
    if _USERNAME_TRAILING_DIGITS.match(ctx.user.name):
        return Signal("Username", f"@{ctx.user.name} — trailing digits", -2)
    return Signal("Username", f"@{ctx.user.name}", 0)


def _signal_onboarding_speed(ctx: AuditContext) -> Signal:
    record = ctx.member_record
    completed_flag = (
        getattr(ctx.member.flags, "completed_onboarding", False)
        if ctx.member else False
    )
    pending = ctx.member.pending if ctx.member else False

    if record is None:
        if completed_flag:
            return Signal("Onboarding speed", "completed (no record — bot was not tracking)", 0)
        return Signal("Onboarding speed", "no record", 0)

    if record.onboarding_completed_at is not None:
        elapsed = (_parse_ts(record.onboarding_completed_at)
                   - _parse_ts(record.joined_at)).total_seconds()
        if elapsed < 5:
            weight, note = -3, "**speedrun**"
        elif elapsed < 30:
            weight, note = -1, "fast, no reading"
        elif elapsed < 30 * 60:
            weight, note = 0, "normal"
        else:
            weight, note = 1, "deliberate"
        return Signal("Onboarding speed", f"{fmt_duration(elapsed)} — {note}", weight)

    if completed_flag:
        return Signal("Onboarding speed",
                       "completed (timing missed — bot was down for the event)", 0)
    if pending:
        return Signal("Onboarding speed", "still pending screening", 0)
    if ctx.member is None:
        return Signal("Onboarding speed", "unknown (user not in current guild)", 0)
    return Signal("Onboarding speed",
                   "not completed or no screening required", 0)


def _signal_first_message_timing(ctx: AuditContext) -> Signal:
    record = ctx.member_record
    if record is None:
        return Signal("First message", "no record", 0)
    if record.first_message_at is not None:
        elapsed = (_parse_ts(record.first_message_at)
                   - _parse_ts(record.joined_at)).total_seconds()
        if elapsed < 30:
            weight, note = -2, "**posted immediately**"
        else:
            weight, note = 0, "posted later"
        return Signal("First message", f"{fmt_duration(elapsed)} after join — {note}", weight)

    joined_ago = (datetime.now(timezone.utc) - _parse_ts(record.joined_at)).total_seconds()
    if joined_ago > 24 * 3600:
        return Signal("First message",
                       "none observed (bot was down or member never posted)", 0)
    return Signal("First message", "none yet", 0)


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


def _compute_audit(ctx: AuditContext) -> Audit:
    signals = [fn(ctx) for fn in _SIGNALS]
    score = sum(s.weight for s in signals)
    return Audit(signals=signals, score=score, band=_band_for(score))


async def _build_context(
    user: discord.User,
    guild_id: int,
    member: Optional[discord.Member],
    full_user: Optional[discord.User],
    used_invite: Optional[discord.Invite],
    client: discord.Client,
) -> AuditContext:
    active_flag = await db.get_active_flag(user.id)
    member_record = await db.get_member_record(user.id, guild_id)
    return AuditContext(
        user=user,
        guild_id=guild_id,
        member=member,
        full_user=full_user,
        active_flag=active_flag,
        used_invite=used_invite,
        total_bot_guilds=len(client.guilds),
        member_record=member_record,
        client=client,
    )


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
    ctx = await _build_context(
        user=member,
        guild_id=member.guild.id,
        member=member,
        full_user=full_user,
        used_invite=used_invite,
        client=client,
    )
    return _compute_audit(ctx)


async def audit_by_user_id(
    user_id: int,
    guild_id: int,
    client: discord.Client,
) -> Optional[Audit]:
    """Audit any Discord user by ID, including banned/left users.

    Returns None if the user ID doesn't resolve to a Discord user at all.
    Member-only signals (server booster, live onboarding state) return
    n/a for users not currently in the guild; historical data from the
    members table still contributes when available.
    """
    try:
        user = await client.fetch_user(user_id)
    except discord.NotFound:
        return None
    except discord.HTTPException:
        return None

    guild = client.get_guild(guild_id)
    member = guild.get_member(user_id) if guild else None

    ctx = await _build_context(
        user=user,
        guild_id=guild_id,
        member=member,
        full_user=user,
        used_invite=None,
        client=client,
    )
    return _compute_audit(ctx)
