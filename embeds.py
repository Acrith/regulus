"""Discord embed formatting for the mod-audit channel."""

from __future__ import annotations

from typing import Union

import discord

from scoring import Audit

_BAND_COLOR = {
    "Trusted":     discord.Color.from_rgb(88, 176, 80),
    "Likely-safe": discord.Color.from_rgb(140, 195, 80),
    "Neutral":     discord.Color.from_rgb(200, 180, 80),
    "Suspicious":  discord.Color.from_rgb(210, 130, 60),
    "Malicious":   discord.Color.from_rgb(200, 60, 60),
}


def build_audit_embed(
    subject: Union[discord.Member, discord.User],
    result: Audit,
) -> discord.Embed:
    created_ts = int(subject.created_at.timestamp())
    embed = discord.Embed(
        title=f"New join: {subject.name}",
        description=(
            f"{subject.mention}  •  ID `{subject.id}`\n"
            f"Created <t:{created_ts}:R>  (<t:{created_ts}:f>)"
        ),
        color=_BAND_COLOR.get(result.band, discord.Color.light_grey()),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_thumbnail(url=subject.display_avatar.url)
    embed.add_field(
        name="Score",
        value=f"**{result.score:+d}**  —  {result.band}",
        inline=False,
    )
    for signal in result.signals:
        weight_suffix = f"  ({signal.weight:+d})" if signal.weight != 0 else ""
        embed.add_field(
            name=signal.name,
            value=f"{signal.detail}{weight_suffix}",
            inline=not signal.prominent,
        )
    return embed
