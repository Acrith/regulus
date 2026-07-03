"""Discord embed formatting for the mod-audit channel."""

from __future__ import annotations

import discord

from scoring import Audit

_BAND_COLOR = {
    "Trusted":     discord.Color.from_rgb(88, 176, 80),
    "Likely-safe": discord.Color.from_rgb(140, 195, 80),
    "Neutral":     discord.Color.from_rgb(200, 180, 80),
    "Suspicious":  discord.Color.from_rgb(210, 130, 60),
    "Malicious":   discord.Color.from_rgb(200, 60, 60),
}


def build_audit_embed(member: discord.Member, result: Audit) -> discord.Embed:
    created_ts = int(member.created_at.timestamp())
    embed = discord.Embed(
        title=f"New join: {member.name}",
        description=(
            f"{member.mention}  •  ID `{member.id}`\n"
            f"Created <t:{created_ts}:R>  (<t:{created_ts}:f>)"
        ),
        color=_BAND_COLOR.get(result.band, discord.Color.light_grey()),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(
        name="Score",
        value=f"**{result.score:+d}**  —  {result.band}",
        inline=False,
    )
    for signal in result.signals:
        # Blocklist row is prominent only when actually flagged
        prominent = signal.prominent and not (
            signal.name == "Blocklist" and signal.weight == 0
        )
        embed.add_field(
            name=signal.name,
            value=f"{signal.detail}  ({signal.weight:+d})",
            inline=not prominent,
        )
    return embed
