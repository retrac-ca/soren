"""
Soren - Discord Calendar & Events Bot
======================================
Main entry point. Loads all cogs (feature modules) and starts the bot.
"""

import discord
from discord.ext import commands
import os
import logging
from dotenv import load_dotenv
from utils.database import init_db

# ── Load environment variables from .env file ──────────────────────────────
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# ── Logging setup ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("soren")

# ── Bot setup ───────────────────────────────────────────────────────────────
# We use discord.Bot (Pycord) for native slash command support
intents = discord.Intents.default()
intents.members = True   # Needed to look up member display names

bot = discord.Bot(intents=intents)

# ── List of cog modules to load ─────────────────────────────────────────────
COGS = [
    "cogs.setup",       # Server setup / configuration
    "cogs.events",      # Event creation, editing, deletion
    "cogs.rsvp",        # RSVP / signup handling
    "cogs.reminders",   # Scheduled reminders
    "cogs.google_cal",  # Google Calendar sync
    "cogs.gcal_integrations",  # G-Cal Integrations — multi-calendar weekly summaries
    "cogs.premium",            # Premium tier checks
]

@bot.event
async def on_ready():
    """Fires when the bot has connected to Discord."""
    log.info(f"Soren is online as {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="your calendar 📅"
        )
    )

@bot.event
async def on_guild_join(guild: discord.Guild):
    """
    Fires when Soren joins a new server.
    Sends a welcome message prompting the admin to run /setup.
    """
    log.info(f"Joined new guild: {guild.name} (ID: {guild.id})")

    # Try to find the best channel to send a welcome message
    channel = guild.system_channel
    if channel is None:
        # Fall back to the first text channel we can write in
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                channel = ch
                break

    if channel:
        embed = discord.Embed(
            title="👋 Thanks for adding Soren!",
            description=(
                "I'm your new calendar & events bot.\n\n"
                "**Before I can be used, a server admin needs to run:**\n"
                "`/setup` — to assign the Event Creator role and configure settings.\n\n"
                "Once setup is complete, use `/help` to see all available commands."
            ),
            color=discord.Color.blurple(),
        )
        await channel.send(embed=embed)


async def load_cogs():
    """Load every cog listed in COGS."""
    for cog in COGS:
        try:
            bot.load_extension(cog)
            log.info(f"Loaded cog: {cog}")
        except Exception as e:
            log.error(f"Failed to load cog {cog}: {e}")


# ── Start ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Initialise the SQLite database (creates tables if they don't exist)
    init_db()
    # Load all feature cogs
    import asyncio
    asyncio.run(load_cogs())  # Pycord supports top-level async load
    bot.run(TOKEN)
