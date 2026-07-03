import json
import logging
import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import db
from embeds import build_audit_embed
from scoring import Audit, audit

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")


def _parse_guilds(raw: str) -> dict[int, int]:
    if not raw or not raw.strip():
        raise SystemExit(
            "GUILDS is empty; expected 'guild_id:mod_channel_id' pairs, comma-separated"
        )
    result: dict[int, int] = {}
    for i, pair in enumerate(raw.split(","), start=1):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise SystemExit(
                f"GUILDS entry #{i} is malformed: '{pair}' "
                "(expected 'guild_id:mod_channel_id')"
            )
        guild_part, channel_part = pair.split(":", 1)
        try:
            guild_id = int(guild_part.strip())
            channel_id = int(channel_part.strip())
        except ValueError:
            raise SystemExit(
                f"GUILDS entry #{i}: guild_id and mod_channel_id must be integers "
                f"(got '{pair}')"
            )
        result[guild_id] = channel_id
    if not result:
        raise SystemExit("GUILDS contained no valid pairs after parsing")
    return result


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
log = logging.getLogger("regulus")

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN missing from .env")
GUILDS: dict[int, int] = _parse_guilds(os.getenv("GUILDS", ""))
log.info("configured guilds (%d): %s",
         len(GUILDS), ", ".join(str(g) for g in GUILDS))

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


async def _record_audit(member: discord.Member, result: Audit, kind: str) -> None:
    signals_data = [
        {"name": s.name, "detail": s.detail, "weight": s.weight}
        for s in result.signals
    ]
    await db.record_audit(
        user_id=member.id,
        guild_id=member.guild.id,
        kind=kind,
        score=result.score,
        band=result.band,
        signals_json=json.dumps(signals_data),
    )


@bot.event
async def setup_hook():
    await db.init()
    log.info("database initialised at %s", db.DB_PATH)
    for guild_id in GUILDS:
        guild = discord.Object(id=guild_id)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        log.info("synced %d slash command(s) to guild %s", len(synced), guild_id)


@bot.event
async def on_ready():
    log.info("logged in as %s (id=%s)", bot.user, bot.user.id)
    for guild in bot.guilds:
        marker = "" if guild.id in GUILDS else "  (not configured — events ignored)"
        log.info("  guild: %s (id=%s, members=%s)%s",
                 guild.name, guild.id, guild.member_count, marker)
        if guild.id in GUILDS:
            channel = bot.get_channel(GUILDS[guild.id])
            if channel is None:
                log.warning("    mod channel %s not visible in this guild",
                            GUILDS[guild.id])
            else:
                log.info("    mod channel: #%s (id=%s)", channel.name, channel.id)


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return
    mod_channel_id = GUILDS.get(member.guild.id)
    if mod_channel_id is None:
        return

    result = await audit(member, bot)
    await _record_audit(member, result, "join")
    log.info("member joined: %s (id=%s, guild=%s, score=%+d, band=%s)",
             member, member.id, member.guild.id, result.score, result.band)

    channel = bot.get_channel(mod_channel_id)
    if channel is None:
        log.error("cannot post audit: mod channel %s unresolved for guild %s",
                  mod_channel_id, member.guild.id)
        return

    embed = build_audit_embed(member, result)
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        log.error("cannot post audit: missing permission in #%s", channel.name)
    except discord.HTTPException as e:
        log.error("failed to post audit embed: %s", e)


async def _reply_with_audit(interaction: discord.Interaction, member: discord.Member) -> None:
    await interaction.response.defer(ephemeral=True)
    result = await audit(member, bot)
    await _record_audit(member, result, "manual")
    embed = build_audit_embed(member, result)
    await interaction.followup.send(embed=embed, ephemeral=True)


async def _post_to_mod_channel(guild_id: int, content: str,
                                embed: Optional[discord.Embed] = None) -> None:
    mod_channel_id = GUILDS.get(guild_id)
    if mod_channel_id is None:
        return
    channel = bot.get_channel(mod_channel_id)
    if channel is None:
        log.warning("mod channel %s unresolved for guild %s", mod_channel_id, guild_id)
        return
    try:
        await channel.send(content=content, embed=embed)
    except discord.Forbidden:
        log.error("cannot post to mod channel: missing permission in #%s", channel.name)
    except discord.HTTPException as e:
        log.error("failed to post to mod channel: %s", e)


@bot.tree.command(
    name="audit",
    description="Run the trust audit on a member and show the result.",
)
@app_commands.describe(member="The member to audit")
@app_commands.default_permissions(moderate_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def audit_command(interaction: discord.Interaction, member: discord.Member) -> None:
    await _reply_with_audit(interaction, member)


@bot.tree.context_menu(name="Audit user")
@app_commands.default_permissions(moderate_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def audit_context_menu(interaction: discord.Interaction, member: discord.Member) -> None:
    await _reply_with_audit(interaction, member)


@bot.tree.command(
    name="flag",
    description="Add a user to the blocklist. Future audits mark them Malicious.",
)
@app_commands.describe(
    member="The member to flag",
    reason="Why they are being flagged (shown in future audits)",
)
@app_commands.default_permissions(moderate_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def flag_command(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "",
) -> None:
    await interaction.response.defer(ephemeral=True)
    reason_text = reason.strip() or None
    await db.add_flag(
        user_id=member.id,
        guild_id=interaction.guild.id,
        flagged_by=interaction.user.id,
        reason=reason_text,
    )
    result = await audit(member, bot)
    await _record_audit(member, result, "flag")
    embed = build_audit_embed(member, result)

    log_content = (
        f"**{interaction.user.mention}** flagged **{member.mention}**"
        + (f" — {reason_text}" if reason_text else "")
    )
    await _post_to_mod_channel(interaction.guild.id, log_content, embed=embed)

    await interaction.followup.send(
        f"Added **{member}** to the blocklist.",
        ephemeral=True,
    )


@bot.tree.command(
    name="unflag",
    description="Remove a user from the blocklist.",
)
@app_commands.describe(member="The member to unflag")
@app_commands.default_permissions(moderate_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def unflag_command(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    await interaction.response.defer(ephemeral=True)
    n = await db.deactivate_flags(member.id)
    if n == 0:
        await interaction.followup.send(
            f"**{member}** had no active flags.",
            ephemeral=True,
        )
        return
    log_content = (
        f"**{interaction.user.mention}** removed **{member.mention}** "
        f"from the blocklist ({n} flag(s) deactivated)."
    )
    await _post_to_mod_channel(interaction.guild.id, log_content)
    await interaction.followup.send(
        f"Removed **{member}** from the blocklist.",
        ephemeral=True,
    )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "You do not have permission to use this command."
    elif isinstance(error, app_commands.NoPrivateMessage):
        message = "This command can only be used in a server."
    else:
        log.exception("app command error: %s", error)
        message = "Something went wrong running that command."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)
