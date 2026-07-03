"""Enforcement decision logic, persistent buttons, and constants."""

from __future__ import annotations

from enum import Enum
from typing import ClassVar

import discord
from discord.ui import Button, DynamicItem, View

import db

MODES = ("shadow", "active")

# Ordered strictest to most lenient. hold_below_band means: bands worse
# than this (strictly to the right in this tuple) get held on @Unverified.
BAND_ORDER = ("Trusted", "Likely-safe", "Neutral", "Suspicious", "Malicious")
HOLD_THRESHOLDS = ("Trusted", "Likely-safe", "Neutral", "Suspicious")

MALICIOUS_ACTIONS = ("kick", "ban", "hold")


class Action(str, Enum):
    NONE = "none"
    HOLD = "hold"
    KICK = "kick"
    BAN = "ban"


def decide_action(band: str, config: db.GuildConfig) -> Action:
    """Map a member's audit band + guild config to the enforcement action."""
    if config.mode == "shadow":
        return Action.NONE
    if band == "Malicious":
        return {
            "kick": Action.KICK,
            "ban": Action.BAN,
            "hold": Action.HOLD,
        }[config.malicious_action]
    if band not in BAND_ORDER:
        return Action.NONE
    threshold_idx = BAND_ORDER.index(config.hold_below_band)
    band_idx = BAND_ORDER.index(band)
    # Bands strictly worse than threshold get held.
    if band_idx > threshold_idx:
        return Action.HOLD
    return Action.NONE


# ---------- Buttons ----------

def _mod_only(interaction: discord.Interaction) -> bool:
    perms = interaction.user.guild_permissions
    return perms.moderate_members


async def _refuse_non_mod(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "You do not have permission to use this button.",
        ephemeral=True,
    )


async def _finalise(interaction: discord.Interaction, action_label: str) -> None:
    """Strike through the notice, append 'X by @mod at time', drop buttons."""
    original = interaction.message.content
    stamp = discord.utils.format_dt(discord.utils.utcnow(), "R")
    new_content = (
        f"~~{original}~~\n**{action_label}** by {interaction.user.mention} {stamp}"
    )
    try:
        await interaction.response.edit_message(content=new_content, view=None)
    except discord.HTTPException:
        pass


class HoldApproveButton(
    DynamicItem[Button],
    template=r"regulus:hold_approve:(?P<user_id>\d+):(?P<guild_id>\d+)",
):
    def __init__(self, user_id: int, guild_id: int) -> None:
        super().__init__(Button(
            style=discord.ButtonStyle.success,
            label="Approve",
            custom_id=f"regulus:hold_approve:{user_id}:{guild_id}",
        ))
        self.user_id = user_id
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["user_id"]), int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _mod_only(interaction):
            await _refuse_non_mod(interaction)
            return
        member = interaction.guild.get_member(self.user_id)
        if member is None:
            await interaction.response.send_message(
                "That user is no longer in the guild.", ephemeral=True,
            )
            return
        config = await db.get_guild_config(interaction.guild.id)
        if config.unverified_role_id is None:
            await interaction.response.send_message(
                "No Unverified role is configured for this guild.", ephemeral=True,
            )
            return
        role = interaction.guild.get_role(config.unverified_role_id)
        if role is None:
            await interaction.response.send_message(
                "The Unverified role is missing from this guild.", ephemeral=True,
            )
            return
        try:
            await member.remove_roles(role, reason=f"Regulus approve by {interaction.user}")
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"Failed to remove role: `{e}`", ephemeral=True,
            )
            return
        await _finalise(interaction, "Approved")


class HoldDenyButton(
    DynamicItem[Button],
    template=r"regulus:hold_deny:(?P<user_id>\d+):(?P<guild_id>\d+)",
):
    def __init__(self, user_id: int, guild_id: int) -> None:
        super().__init__(Button(
            style=discord.ButtonStyle.danger,
            label="Deny",
            custom_id=f"regulus:hold_deny:{user_id}:{guild_id}",
        ))
        self.user_id = user_id
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["user_id"]), int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _mod_only(interaction):
            await _refuse_non_mod(interaction)
            return
        member = interaction.guild.get_member(self.user_id)
        if member is None:
            await interaction.response.send_message(
                "That user is no longer in the guild.", ephemeral=True,
            )
            return
        await db.add_flag(
            user_id=self.user_id,
            guild_id=self.guild_id,
            flagged_by=interaction.user.id,
            reason="denied on join via audit button",
        )
        try:
            await member.kick(reason=f"Regulus deny by {interaction.user}")
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"Failed to kick: `{e}`", ephemeral=True,
            )
            return
        await _finalise(interaction, "Denied (kicked + flagged)")


class HoldWatchButton(
    DynamicItem[Button],
    template=r"regulus:hold_watch:(?P<user_id>\d+):(?P<guild_id>\d+)",
):
    def __init__(self, user_id: int, guild_id: int) -> None:
        super().__init__(Button(
            style=discord.ButtonStyle.secondary,
            label="Watch",
            custom_id=f"regulus:hold_watch:{user_id}:{guild_id}",
        ))
        self.user_id = user_id
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["user_id"]), int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _mod_only(interaction):
            await _refuse_non_mod(interaction)
            return
        # Watch leaves the Unverified role in place; the decision is
        # recorded on the notice. Behavioural escalation for watched
        # members lands with the hot-triggers commit.
        await _finalise(interaction, "Watching")


class UndoBanButton(
    DynamicItem[Button],
    template=r"regulus:undo_ban:(?P<user_id>\d+):(?P<guild_id>\d+)",
):
    def __init__(self, user_id: int, guild_id: int) -> None:
        super().__init__(Button(
            style=discord.ButtonStyle.success,
            label="Undo ban",
            custom_id=f"regulus:undo_ban:{user_id}:{guild_id}",
        ))
        self.user_id = user_id
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["user_id"]), int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _mod_only(interaction):
            await _refuse_non_mod(interaction)
            return
        try:
            await interaction.guild.unban(
                discord.Object(id=self.user_id),
                reason=f"Regulus undo by {interaction.user}",
            )
        except discord.NotFound:
            pass  # already unbanned
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"Failed to unban: `{e}`", ephemeral=True,
            )
            return
        n = await db.deactivate_flags(self.user_id)
        note = f"unbanned + {n} flag(s) cleared" if n else "unbanned"
        await _finalise(interaction, f"Undone ({note})")


class UndoKickButton(
    DynamicItem[Button],
    template=r"regulus:undo_kick:(?P<user_id>\d+):(?P<guild_id>\d+)",
):
    def __init__(self, user_id: int, guild_id: int) -> None:
        super().__init__(Button(
            style=discord.ButtonStyle.success,
            label="Undo",
            custom_id=f"regulus:undo_kick:{user_id}:{guild_id}",
        ))
        self.user_id = user_id
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["user_id"]), int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _mod_only(interaction):
            await _refuse_non_mod(interaction)
            return
        # A kick has no persistent state to reverse — the user can rejoin
        # freely. All we can do is clear any auto-flags that would trigger
        # blocklist on the rejoin audit.
        n = await db.deactivate_flags(self.user_id)
        note = (
            f"{n} auto-flag(s) cleared — user may rejoin normally"
            if n else "nothing to reverse — user was only kicked"
        )
        await _finalise(interaction, f"Undone ({note})")


ALL_DYNAMIC_ITEMS: ClassVar[tuple] = (
    HoldApproveButton,
    HoldDenyButton,
    HoldWatchButton,
    UndoBanButton,
    UndoKickButton,
)


def hold_view(user_id: int, guild_id: int) -> View:
    view = View(timeout=None)
    view.add_item(HoldApproveButton(user_id, guild_id))
    view.add_item(HoldDenyButton(user_id, guild_id))
    view.add_item(HoldWatchButton(user_id, guild_id))
    return view


def undo_view(user_id: int, guild_id: int, action: Action) -> View:
    view = View(timeout=None)
    if action is Action.BAN:
        view.add_item(UndoBanButton(user_id, guild_id))
    else:
        view.add_item(UndoKickButton(user_id, guild_id))
    return view
