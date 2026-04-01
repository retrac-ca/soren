"""
cogs/ping.py
=============
/ping — Check Soren's status, latency, and bot info.
"""

import discord
from discord.ext import commands
from datetime import datetime, timezone
import platform
import logging

log = logging.getLogger("soren.ping")

BOT_VERSION = "1.2"
SUPPORT_URL = "https://soren.retrac.ca"
CREATOR     = "Toadle"


class Ping(commands.Cog):
    """Bot status and info command."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="ping", description="Check Soren's status and response time.")
    async def ping(self, ctx: discord.ApplicationContext):
        """Shows bot latency, uptime, server count, and info."""

        # Latency
        latency_ms = round(self.bot.latency * 1000)

        # Uptime
        start_time = getattr(self.bot, "start_time", None)
        if start_time:
            delta = datetime.now(timezone.utc) - start_time
            total_seconds = int(delta.total_seconds())
            hours, remainder = divmod(total_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            uptime_str = f"{hours}h {minutes}m {seconds}s"
        else:
            uptime_str = "Unknown"

        # Latency color indicator
        if latency_ms < 100:
            indicator = "🟢"
            color = discord.Color.from_rgb(87, 242, 135)
        elif latency_ms < 200:
            indicator = "🟡"
            color = discord.Color.from_rgb(255, 184, 28)
        else:
            indicator = "🔴"
            color = discord.Color.from_rgb(237, 66, 69)

        embed = discord.Embed(
            title="🏓  Pong!",
            color=color,
        )
        embed.add_field(
            name="Latency",
            value=f"{indicator} `{latency_ms}ms`",
            inline=True,
        )
        embed.add_field(
            name="Uptime",
            value=f"⏱️ `{uptime_str}`",
            inline=True,
        )
        embed.add_field(
            name="Servers",
            value=f"🌐 `{len(self.bot.guilds)}`",
            inline=True,
        )
        embed.add_field(
            name="Version",
            value=f"📦 `v{BOT_VERSION}`",
            inline=True,
        )
        embed.add_field(
            name="Creator",
            value=f"👤 {CREATOR}",
            inline=True,
        )
        embed.add_field(
            name="Support",
            value=f"🔗 {SUPPORT_URL}",
            inline=True,
        )
        embed.set_footer(
            text=f"Python {platform.python_version()}  •  py-cord"
        )

        await ctx.respond(embed=embed)


def setup(bot: discord.Bot):
    bot.add_cog(Ping(bot))