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
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
MOD_CHANNEL_ID = int(os.getenv("MOD_CHANNEL_ID", "0"))

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN missing from .env")
if not GUILD_ID:
    raise SystemExit("GUILD_ID missing from .env")
if not MOD_CHANNEL_ID:
    raise SystemExit("MOD_CHANNEL_ID missing from .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
log = logging.getLogger("regulus")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
_GUILD = discord.Object(id=GUILD_ID)


@bot.event
async def setup_hook():
    synced = await bot.tree.sync(guild=_GUILD)
    log.info("synced %d slash command(s) to guild %s", len(synced), GUILD_ID)


@bot.event
async def on_ready():
    log.info("logged in as %s (id=%s)", bot.user, bot.user.id)
    for guild in bot.guilds:
        log.info("  connected to guild: %s (id=%s, members=%s)",
                 guild.name, guild.id, guild.member_count)
    channel = bot.get_channel(MOD_CHANNEL_ID)
    if channel is None:
        log.warning("mod channel id=%s not visible to bot; audit posts will fail",
                    MOD_CHANNEL_ID)
    else:
        log.info("  mod channel: #%s (id=%s)", channel.name, channel.id)


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return

    result = audit(member)
    log.info("member joined: %s (id=%s, score=%+d, band=%s)",
             member, member.id, result.score, result.band)

    channel = bot.get_channel(MOD_CHANNEL_ID)
    if channel is None:
        log.error("cannot post audit: mod channel %s unresolved", MOD_CHANNEL_ID)
        return

    embed = build_audit_embed(member, result)
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        log.error("cannot post audit: missing permission in #%s", channel.name)
    except discord.HTTPException as e:
        log.error("failed to post audit embed: %s", e)


async def _reply_with_audit(interaction: discord.Interaction, member: discord.Member) -> None:
    result = audit(member)
    embed = build_audit_embed(member, result)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="audit",
    description="Run the trust audit on a member and show the result.",
    guild=_GUILD,
)
@app_commands.describe(member="The member to audit")
@app_commands.default_permissions(manage_messages=True)
async def audit_command(interaction: discord.Interaction, member: discord.Member) -> None:
    await _reply_with_audit(interaction, member)


@bot.tree.context_menu(name="Audit user", guild=_GUILD)
@app_commands.default_permissions(manage_messages=True)
async def audit_context_menu(interaction: discord.Interaction, member: discord.Member) -> None:
    await _reply_with_audit(interaction, member)


if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)
