import logging
import os

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from embeds import build_audit_embed
from scoring import audit

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


if not TOKEN:
    raise SystemExit("DISCORD_TOKEN missing from .env")
GUILDS: dict[int, int] = _parse_guilds(os.getenv("GUILDS", ""))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
log = logging.getLogger("regulus")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def setup_hook():
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
    embed = build_audit_embed(member, result)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="audit",
    description="Run the trust audit on a member and show the result.",
)
@app_commands.describe(member="The member to audit")
@app_commands.default_permissions(manage_messages=True)
async def audit_command(interaction: discord.Interaction, member: discord.Member) -> None:
    await _reply_with_audit(interaction, member)


@bot.tree.context_menu(name="Audit user")
@app_commands.default_permissions(manage_messages=True)
async def audit_context_menu(interaction: discord.Interaction, member: discord.Member) -> None:
    await _reply_with_audit(interaction, member)


if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)
