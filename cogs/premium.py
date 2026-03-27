"""
cogs/premium.py
================
Premium tier enforcement helpers and the /premium and /help commands.

Tier enforcement happens in:
  - cogs/events.py  → event creation limit (5 for free)
  - cogs/rsvp.py    → RSVP display cap (50 per status for free)

This cog exposes:
  /premium  — shows what premium unlocks
  /help     — lists all available commands
"""

import discord
from discord.ext import commands
from utils.database import is_premium, get_guild_config
from utils.embeds import COLOR_EVENT


class Premium(commands.Cog):
    """Premium info and general help commands."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    # ── /premium ───────────────────────────────────────────────────────────
    @discord.slash_command(name="premium", description="Learn about Soren Premium.")
    async def premium(self, ctx: discord.ApplicationContext):
        """Displays the free vs premium feature comparison."""
        server_tier = "⭐ Premium" if is_premium(ctx.guild.id) else "Free"

        embed = discord.Embed(
            title="⭐  Soren Premium",
            description=(
                f"This server is currently on the **{server_tier}** plan.\n\n"
                "Upgrade to Premium to unlock the full experience."
            ),
            color=discord.Color.gold(),
        )

        embed.add_field(
            name="Feature",
            value=(
                "📅 Events per server\n"
                "👥 RSVP list per event\n"
                "🔁 Recurring events\n"
                "📆 Google Calendar sync\n"
            ),
            inline=True,
        )
        embed.add_field(
            name="Free",
            value="5\n50 shown\n✅\n✅",
            inline=True,
        )
        embed.add_field(
            name="Premium",
            value="Unlimited\nUnlimited\n✅\n✅",
            inline=True,
        )
        embed.set_footer(text="Contact the bot owner or visit our website to upgrade.")
        await ctx.respond(embed=embed)

    # ── /help ──────────────────────────────────────────────────────────────
    @discord.slash_command(name="help", description="Show all Soren commands.")
    async def help(self, ctx: discord.ApplicationContext):
        """Lists every available slash command with a brief description."""
        embed = discord.Embed(
            title="📖  Soren — Command Reference",
            color=COLOR_EVENT,
        )

        embed.add_field(
            name="⚙️  Setup (Admins)",
            value=(
                "`/setup` — First-time server configuration\n"
                "`/config` — View current settings\n"
                "`/setpremium` — Toggle premium *(bot owner only)*"
            ),
            inline=False,
        )
        embed.add_field(
            name="📅  Events (Event Creator role required)",
            value=(
                "`/newevent` — Create a new event\n"
                "`/editevent` — Edit an existing event by ID\n"
                "`/deleteevent` — Delete an event by ID\n"
                "`/listevents` — View all upcoming events"
            ),
            inline=False,
        )
        embed.add_field(
            name="📆  Google Calendar (Admins)",
            value=(
                "`/gcal connect` — Link a Google Calendar\n"
                "`/gcal verify` — Complete the connection with your auth code\n"
                "`/gcal disconnect` — Remove the Google Calendar link"
            ),
            inline=False,
        )
        embed.add_field(
            name="📆  G-Cal Integrations (Admins)",
            value=(
                "`/gcalint add` — Connect a Google Calendar for auto-summaries\n"
                "`/gcalint verify` — Complete the OAuth connection\n"
                "`/gcalint list` — View all connected calendars\n"
                "`/gcalint remove` — Disconnect a calendar\n"
                "`/gcalint pause` — Pause or resume a calendar\n"
                "`/gcalint post` — Manually trigger a summary post"
            ),
            inline=False,
        )
        embed.add_field(
            name="💎  Misc",
            value=(
                "`/premium` — Free vs Premium comparison\n"
                "`/help` — This message"
            ),
            inline=False,
        )
        embed.set_footer(text="RSVP to events using the ✅ ❓ ❌ buttons on event posts.")
        await ctx.respond(embed=embed, ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(Premium(bot))
