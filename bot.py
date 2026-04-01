"""
Soren - Discord Calendar & Events Bot
======================================
Main entry point. Loads all cogs (feature modules) and starts the bot.
"""

import discord
from discord.ext import commands
import os
import logging
import asyncio
from datetime import datetime, timezone
from dotenv import load_dotenv
from utils.database import init_db

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("soren")

intents = discord.Intents.default()
intents.members = True

bot = discord.Bot(intents=intents)

COGS = [
    "cogs.setup",
    "cogs.events",
    "cogs.rsvp",
    "cogs.reminders",
    "cogs.google_cal",
    "cogs.gcal_integrations",
    "cogs.premium",
    "cogs.ping",
]


@bot.event
async def on_ready():
    """Fires when the bot has connected to Discord."""
    bot.start_time = datetime.now(timezone.utc)
    log.info(f"Soren is online as {bot.user} (ID: {bot.user.id})")

    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="your calendar 📅"
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
    log.info(f"Joined new guild: {guild.name} (ID: {guild.id})")

    channel = guild.system_channel
    if channel is None:
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