"""
Soren - Discord Calendar & Events Bot
======================================
Main entry point. Loads all cogs (feature modules) and starts the bot.
"""

import discord
from discord.ext import commands
import os
import logging
import logging.handlers
import asyncio
from datetime import datetime, timezone
from dotenv import load_dotenv
from utils.database import init_db

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# ── Logging setup ─────────────────────────────────────────────────────────────
# Logs go to both the console (stdout) and a daily rotating file.
# A new log file is created each day: logs/soren_YYYY_MM_DD.log
# Files are kept for 30 days before being automatically removed.

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DIR     = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)


class DailyRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """
    Rotates log files at midnight each day.
    Files are named soren_YYYY_MM_DD.log.
    Old files beyond backup_count days are removed automatically.
    """

    def __init__(self, log_dir: str, backup_count: int = 30):
        filename = self._day_filename(log_dir)
        super().__init__(
            filename=filename,
            when="midnight",
            interval=1,
            backupCount=backup_count,
            encoding="utf-8",
            delay=False,
        )
        self.log_dir = log_dir

    @staticmethod
    def _day_filename(log_dir: str) -> str:
        now = datetime.now()
        return os.path.join(log_dir, f"soren_{now.strftime('%Y_%m_%d')}.log")

    def shouldRollover(self, record) -> int:
        """Roll over when the calendar day changes."""
        expected = self._day_filename(self.log_dir)
        return 1 if self.baseFilename != expected else 0

    def doRollover(self):
        """Switch to the new day's file."""
        if self.stream:
            self.stream.close()
            self.stream = None
        self.baseFilename = self._day_filename(self.log_dir)
        self.stream = self._open()


def _setup_logging():
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    formatter = logging.Formatter(LOG_FORMAT)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Daily file handler
    file_handler = DailyRotatingFileHandler(LOG_DIR, backup_count=30)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


_setup_logging()
log = logging.getLogger("soren")

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents         = discord.Intents.default()
intents.members = True

bot = discord.Bot(intents=intents)

COGS = [
    "cogs.setup",
    "cogs.events",
    "cogs.rsvp",
    "cogs.reminders",
    "cogs.gcal_integrations",
    "cogs.premium",
    "cogs.ping",
    "cogs.modlogs",
]


@bot.event
async def on_ready():
    """Fires when the bot has connected to Discord."""
    bot.start_time = datetime.now(timezone.utc)
    log.info(f"Soren is online as {bot.user} (ID: {bot.user.id})")
    log.info(f"Serving {len(bot.guilds)} guild(s)")

    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="your calendar 📅",
        )
    )

    asyncio.create_task(_sync_commands())


async def _sync_commands():
    """Sync slash commands after a short delay to let the gateway settle."""
    await asyncio.sleep(3)
    try:
        await bot.sync_commands()
        log.info("Slash commands synced successfully.")
    except Exception as e:
        log.error(f"Failed to sync slash commands: {e}")


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Fires when Soren joins a new server."""
    log.info(f"Joined new guild: {guild.name} (ID: {guild.id}) — now in {len(bot.guilds)} guild(s)")

    channel = guild.system_channel
    if channel is None:
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                channel = ch
                break

    if channel:
        embed = discord.Embed(
            title="👋  Thanks for adding Soren!",
            description=(
                "I'm your new calendar and events bot.\n\n"
                "**Before I can be used, a server admin needs to run:**\n"
                "`/setup` — to assign the Event Creator role and configure settings.\n\n"
                "Once setup is complete, use `/help` to see all available commands.\n\n"
                "Need help? Visit **https://soren.retrac.ca**"
            ),
            color=discord.Color.blurple(),
        )
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            log.warning(f"Could not send welcome message in guild {guild.id} — missing permissions in channel {channel.id}")


@bot.event
async def on_guild_remove(guild: discord.Guild):
    """Fires when Soren is removed from a server."""
    log.info(f"Removed from guild: {guild.name} (ID: {guild.id}) — now in {len(bot.guilds)} guild(s)")


def load_cogs():
    """Load every cog listed in COGS."""
    for cog in COGS:
        try:
            bot.load_extension(cog)
            log.info(f"Loaded cog: {cog}")
        except Exception as e:
            log.error(f"Failed to load cog {cog}: {e}")


if __name__ == "__main__":
    init_db()
    load_cogs()
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        log.info("Soren shut down via Ctrl+C.")