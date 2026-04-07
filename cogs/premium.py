"""
cogs/premium.py
================
Premium tier enforcement helpers and the /premium, /help,
and /premiumcode commands.
"""

import discord
from discord.ext import commands
import os
import logging

from utils.database import is_premium, get_connection, upsert_guild_config
from utils.embeds import COLOR_EVENT, build_error_embed, build_success_embed
from cogs.events import FREE_EVENT_LIMIT, PREMIUM_EVENT_LIMIT

log = logging.getLogger("soren.premium")

KEYS_FILE = os.path.join(os.path.dirname(__file__), "..", "premium_keys.txt")


def load_valid_keys() -> set[str]:
    if not os.path.exists(KEYS_FILE):
        log.warning("premium_keys.txt not found — no premium codes will be valid")
        return set()
    with open(KEYS_FILE, "r") as f:
        return {
            line.strip().upper()
            for line in f
            if line.strip() and not line.strip().startswith("#")
        }


def is_redeemed(code: str) -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM redeemed_codes WHERE code=?", (code,)).fetchone()
    return row is not None


def mark_redeemed(code: str, guild_id: int):
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO redeemed_codes (code, used_by_guild) VALUES (?, ?)",
            (code, guild_id),
        )
        conn.commit()


class Premium(commands.Cog):
    """Premium info and general help commands."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    # ── /premiumcode ──────────────────────────────────────────────────────
    @discord.slash_command(name="premiumcode", description="Redeem a premium code to unlock Soren Premium for this server.")
    @discord.default_permissions(administrator=True)
    async def premiumcode(
        self,
        ctx: discord.ApplicationContext,
        code: discord.Option(str, description="Your premium redemption code.", required=True),
    ):
        code       = code.strip().upper()
        valid_keys = load_valid_keys()

        if code not in valid_keys:
            log.warning(f"Invalid premium code attempt: '{code}' in guild {ctx.guild.id} by {ctx.author}")
            await ctx.respond(
                embed=build_error_embed("That code is invalid. Please double-check and try again."),
                ephemeral=True,
            )
            return

        if is_redeemed(code):
            log.warning(f"Already-redeemed code attempt: '{code}' in guild {ctx.guild.id} by {ctx.author}")
            await ctx.respond(
                embed=build_error_embed("That code has already been redeemed and cannot be used again."),
                ephemeral=True,
            )
            return

        mark_redeemed(code, ctx.guild.id)
        upsert_guild_config(ctx.guild.id, is_premium=1)
        log.info(f"Premium code redeemed in guild {ctx.guild.id} ({ctx.guild.name}) by {ctx.author}")

        embed = discord.Embed(
            title="\u2b50  Premium Activated!",
            description=(
                f"**{ctx.guild.name}** now has access to Soren Premium!\n\n"
                "**Unlocked:**\n"
                f"• Up to **{PREMIUM_EVENT_LIMIT}** active events\n"
                "• Up to **100** RSVP names displayed\n"
                "• All 8 embed colors\n"
                "• Up to **20** G-Cal integrations\n"
                "• Mention/remind up to **3 roles** per event\n"
                "• Custom RSVP button labels\n\n"
                "Run `/config` to confirm your server's plan."
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"Code redeemed by {ctx.author.display_name}")
        await ctx.respond(embed=embed)

    # ── /premium ──────────────────────────────────────────────────────────
    @discord.slash_command(name="premium", description="Learn about Soren Premium.")
    async def premium(self, ctx: discord.ApplicationContext):
        is_prem     = is_premium(ctx.guild.id)
        server_tier = "\u2b50 Premium" if is_prem else "Free"

        embed = discord.Embed(
            title="\u2b50  Soren Premium",
            description=(
                f"This server is currently on the **{server_tier}** plan.\n\n"
                "Soren Premium gives your server access to expanded limits and exclusive features. "
                "Visit **[soren.retrac.ca](https://soren.retrac.ca)** to learn more and get started."
            ),
            color=discord.Color.gold(),
        )

        embed.add_field(
            name="Feature",
            value=(
                "\U0001f4c5 Active events\n"
                "\U0001f465 RSVP names shown\n"
                "\U0001f3a8 Embed color options\n"
                "\U0001f501 Recurring events\n"
                "\U0001f4c6 Google Calendar sync\n"
                "\U0001f4c6 G-Cal integrations\n"
                "\U0001f514 Mention/remind roles\n"
                "\U0001f3f7\ufe0f Custom button labels"
            ),
            inline=True,
        )
        embed.add_field(
            name="Free",
            value=(
                f"{FREE_EVENT_LIMIT}\n"
                "50\n"
                "3 colors\n"
                "\u2705\n"
                "\u2705\n"
                "Up to 5\n"
                "1 role\n"
                "\u274c"
            ),
            inline=True,
        )
        embed.add_field(
            name="\u2b50 Premium",
            value=(
                f"{PREMIUM_EVENT_LIMIT}\n"
                "100\n"
                "8 colors\n"
                "\u2705\n"
                "\u2705\n"
                "Up to 20\n"
                "Up to 3 roles\n"
                "\u2705"
            ),
            inline=True,
        )

        embed.add_field(
            name="\U0001f4b3  How to Purchase",
            value=(
                "Visit **[soren.retrac.ca](https://soren.retrac.ca)** to purchase a license.\n"
                "Once purchased you'll receive a premium code to activate here with `/premiumcode`."
            ),
            inline=False,
        )

        embed.add_field(
            name="\U0001f4b0  Where Your Money Goes",
            value=(
                "Your purchase goes directly toward covering the server hosting costs "
                "that keep Soren running. Any revenue beyond that provides the incentive "
                "to keep developing new features and push updates out faster."
            ),
            inline=False,
        )

        if is_prem:
            embed.set_footer(text="\u2705 This server already has Premium \u2014 thank you for your support!")
        else:
            embed.set_footer(text="Visit soren.retrac.ca to learn more and get started.")

        await ctx.respond(embed=embed)

    # ── /help ─────────────────────────────────────────────────────────────
    @discord.slash_command(name="help", description="Show all Soren commands.")
    async def help(self, ctx: discord.ApplicationContext):
        embed = discord.Embed(title="\U0001f4d6  Soren \u2014 Command Reference", color=COLOR_EVENT)

        embed.add_field(
            name="\u2699\ufe0f  Setup (Admins)",
            value=(
                "`/setup` \u2014 First-time server configuration\n"
                "`/config` \u2014 View current settings\n"
                "`/embedcolor` \u2014 Choose event embed color"
            ),
            inline=False,
        )
        embed.add_field(
            name="\U0001f4c5  Events (Event Creator role required)",
            value=(
                "`/newevent` \u2014 Create a new event\n"
                "`/editeventdetails` \u2014 Edit title, description, max RSVPs, notify role\n"
                "`/editeventtime` \u2014 Edit start/end time, timezone, reminder\n"
                "`/deleteevent` \u2014 Delete an event\n"
                "`/cancelevent` \u2014 Soft cancel an event\n"
                "`/listevents` \u2014 View all upcoming events\n"
                "`/myevents` \u2014 View events you've RSVPed to\n"
                "`/eventbuttons` \u2014 Customize RSVP button labels\n"
                "`/exportevents` \u2014 Export events as .ics calendar file"
            ),
            inline=False,
        )
        embed.add_field(
            name="\U0001f4c6  Google Calendar (Admins)",
            value=(
                "`/gcal connect` \u2014 Link a Google Calendar\n"
                "`/gcal verify` \u2014 Complete the connection\n"
                "`/gcal disconnect` \u2014 Remove the Google Calendar link"
            ),
            inline=False,
        )
        embed.add_field(
            name="\U0001f4c6  G-Cal Integrations (Admins)",
            value=(
                "`/gcalint add` \u2014 Connect a calendar for auto-summaries\n"
                "`/gcalint verify` \u2014 Complete the OAuth connection\n"
                "`/gcalint list` \u2014 View all connected calendars\n"
                "`/gcalint remove` \u2014 Disconnect a calendar\n"
                "`/gcalint pause` \u2014 Pause or resume a calendar\n"
                "`/gcalint post` \u2014 Manually trigger a summary post"
            ),
            inline=False,
        )
        embed.add_field(
            name="\U0001f48e  Premium",
            value=(
                "`/premiumcode` \u2014 Redeem a premium code\n"
                "`/premium` \u2014 Free vs Premium comparison\n"
                "`/ping` \u2014 Bot status and latency\n"
                "`/help` \u2014 This message"
            ),
            inline=False,
        )
        embed.set_footer(text="RSVP to events using the buttons on event posts.")
        await ctx.respond(embed=embed, ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(Premium(bot))