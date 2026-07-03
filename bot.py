import os
import logging

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN missing from .env")

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
async def on_ready():
    log.info("logged in as %s (id=%s)", bot.user, bot.user.id)
    for guild in bot.guilds:
        log.info("  connected to guild: %s (id=%s, members=%s)",
                 guild.name, guild.id, guild.member_count)


@bot.event
async def on_member_join(member: discord.Member):
    log.info("member joined: %s (id=%s, created=%s)",
             member, member.id, member.created_at.isoformat())


if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)
