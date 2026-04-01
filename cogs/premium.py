"""
cogs/premium.py
================
Premium tier enforcement helpers and the /premium, /help,
and /premiumcode commands.

/premiumcode — lets a server admin redeem a code from premium_keys.txt
               to activate premium on their server. Codes are single-use;
               redeemed codes are tracked in the redeemed_codes DB table.
"""

import discord
from discord.ext import commands
from datetime import datetime, timezone
import os
import logging

from utils.database import is_premium, get_connection, upsert_guild_config
from utils.embeds import COLOR_EVENT, build_error_embed, build_success_embed

log = logging.getLogger("soren.premium")

# Path to the premium keys file — sits next to bot.py
KEYS_FILE = os.path.join(os.path.dirname(__file__), "..", "premium_keys.txt")


def load_valid_keys() -> set[str]:
    """
    Read premium_keys.txt and return a set of uppercased codes.
    Returns an empty set if the file doesn't exist.
    """
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
    """Return True if this code has already been used."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM redeemed_codes WHERE code=?", (code,)
        ).fetchone()
    return row is not None


def mark_redeemed(code: str, guild_id: int):
    """Record that this code has been redeemed by a guild."""
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

    # ── /premiumcode ───────────────────────────────────────────────────────
    @discord.slash_command(
        name="premiumcode",
        description="Redeem a premium code to unlock Soren Premium for this server."
    )
    @discord.default_permissions(administrator=True)
    async def premiumcode(
        self,
        ctx: discord.ApplicationContext,
        code: discord.Option(str, description="Your premium redemption code.", required=True),
    ):
        code = code.strip().upper()

        valid_keys = load_valid_keys()

        # Code not in the keys file
        if code not in valid_keys:
            log.warning(
                f"Invalid premium code attempt: '{code}' "
                f"in guild {ctx.guild.id} by {ctx.author}"
            )
            await ctx.respond(
                embed=build_error_embed(
                    "That code is invalid. Please double-check and try again."
                ),
                ephemeral=True,
            )
            return

        # Code already redeemed
        if is_redeemed(code):
            log.warning(
                f"Already-redeemed code attempt: '{code}' "
                f"in guild {ctx.guild.id} by {ctx.author}"
            )
            await ctx.respond(
                embed=build_error_embed(
                    "That code has already been redeemed and cannot be used again."
                ),
                ephemeral=True,
            )
            return

        # Valid and unused — activate premium
        mark_redeemed(code, ctx.guild.id)
        upsert_guild_config(ctx.guild.id, is_premium=1)

        log.info(
            f"Premium code redeemed in guild {ctx.guild.id} "
            f"({ctx.guild.name}) by {ctx.author}"
        )

        embed = discord.Embed(
            title="⭐  Premium Activated!",
            description=(
                f"**{ctx.guild.name}** now has access to Soren Premium!\n\n"
                "**Unlocked:**\n"
                "• Unlimited events\n"
                "• Unlimited RSVP list display\n\n"
                "Run `/config` to confirm your server's plan."
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"Code redeemed by {ctx.author.display_name}")
        await ctx.respond(embed=embed)

    # ── /premium ───────────────────────────────────────────────────────────
    @discord.slash_command(name="premium", description="Learn about Soren Premium.")
    async def premium(self, ctx: discord.ApplicationContext):
        server_tier = "\u2b50 Premium" if is_premium(ctx.guild.id) else "Free"
        is_prem = is_premium(ctx.guild.id)

        embed = discord.Embed(
            title="\u2b50  Soren Premium",
            description=(
                f"This server is currently on the **{server_tier}** plan.\n\n"
                "Soren Premium is a **one-time purchase of $15 per server** \u2014 "
                "no subscriptions, no renewals. You get lifetime access to all "
                "current and future premium features, plus support if you need it."
            ),
            color=discord.Color.gold(),
        )

        embed.add_field(
            name="Feature",
            value=(
                "\U0001f4c5 Events per server\n"
                "\U0001f465 RSVP names shown\n"
                "\U0001f3a8 Embed color options\n"
                "\U0001f501 Recurring events\n"
                "\U0001f4c6 Google Calendar sync\n"
                "\U0001f4c6 G-Cal integrations\n"
                "\U0001f3f7\ufe0f Custom button labels"
            ),
            inline=True,
        )
        embed.add_field(
            name="Free",
            value="10\n50\n3 colors\n\u2705\nUp to 5\n\u274c",
            inline=True,
        )
        embed.add_field(
            name="\u2b50 Premium",
            value="Unlimited\nUnlimited\n8 colors\n\u2705\nUnlimited\n\u2705 *(coming soon)*",
            inline=True,
        )

        embed.add_field(
            name="\U0001f4b3  How to Purchase",
            value=(
                "Visit **[soren.retrac.ca](https://soren.retrac.ca)** to purchase a license.\n"
                "Once purchased you\'ll receive a premium code to activate here with `/premiumcode`."
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
            embed.set_footer(text="soren.retrac.ca  \u2022  $15 one-time  \u2022  Lifetime updates & support")

        await ctx.respond(embed=embed)

    # ── /help ──────────────────────────────────────────────────────────────
    @discord.slash_command(name="help", description="Show all Soren commands.")
    async def help(self, ctx: discord.ApplicationContext):
        embed = discord.Embed(
            title="📖  Soren — Command Reference",
            color=COLOR_EVENT,
        )
        embed.add_field(
            name="⚙️  Setup (Admins)",
            value=(
                "`/setup` — First-time server configuration\n"
                "`/config` — View current settings"
            ),
            inline=False,
        )
        embed.add_field(
            name="📅  Events (Event Creator role required)",
            value=(
                "`/newevent` — Create a new event\n"
                "`/editevent` — Edit an existing event by ID\n"
                "`/deleteevent` — Delete an event by ID\n"
                "`/listevents` — View all upcoming events\n"
                "`/eventbuttons` — Toggle Tentative button on an event"
            ),
            inline=False,
        )
        embed.add_field(
            name="📆  Google Calendar (Admins)",
            value=(
                "`/gcal connect` — Link a Google Calendar\n"
                "`/gcal verify` — Complete the connection\n"
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
            name="💎  Premium",
            value=(
                "`/premiumcode` — Redeem a premium code\n"
                "`/premium` — Free vs Premium comparison\n"
                "`/help` — This message"
            ),
            inline=False,
        )
        embed.set_footer(text="RSVP to events using the ✅ ❓ ❌ buttons on event posts.")
        await ctx.respond(embed=embed, ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(Premium(bot))