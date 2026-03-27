"""
cogs/setup.py
==============
Handles the /setup command that server admins run when Soren first joins.
Also provides /config to view current settings and /setpremium (owner only).
"""

import discord
from discord.ext import commands
from discord import SlashCommandGroup
from utils.database import upsert_guild_config, get_guild_config
from utils.embeds import build_error_embed, build_success_embed
import os

# The Discord user ID of the bot owner — used to gate /setpremium
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0"))


class Setup(commands.Cog):
    """Server setup and configuration commands."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    # ── /setup ────────────────────────────────────────────────────────────
    @discord.slash_command(
        name="setup",
        description="Configure Soren for your server. (Admin only)"
    )
    @discord.default_permissions(administrator=True)   # Only admins see this
    async def setup(
        self,
        ctx: discord.ApplicationContext,
        event_role: discord.Option(
            discord.Role,
            description="The role whose members can create/edit/delete events.",
            required=True,
        ),
    ):
        """
        First-time setup command.
        Assigns the Event Creator role and creates the guild config row.
        """
        # Save the config to the database
        upsert_guild_config(ctx.guild.id, creator_role_id=event_role.id)

        embed = discord.Embed(
            title="✅  Soren is ready!",
            description=(
                f"**Event Creator Role:** {event_role.mention}\n\n"
                "Members with this role (plus server admins) can now:\n"
                "• Create events with `/newevent`\n"
                "• Edit events with `/editevent`\n"
                "• Delete events with `/deleteevent`\n\n"
                "Everyone else can RSVP to events using the buttons on event posts.\n\n"
                "Run `/config` any time to review or change these settings."
            ),
            color=discord.Color.green(),
        )
        await ctx.respond(embed=embed)

    # ── /config ───────────────────────────────────────────────────────────
    @discord.slash_command(
        name="config",
        description="View Soren's current configuration for this server."
    )
    @discord.default_permissions(administrator=True)
    async def config(self, ctx: discord.ApplicationContext):
        """Shows the current server configuration."""
        cfg = get_guild_config(ctx.guild.id)

        if not cfg:
            await ctx.respond(
                embed=build_error_embed("Soren hasn't been set up yet. Run `/setup` first."),
                ephemeral=True,
            )
            return

        # Resolve role name from ID
        role = ctx.guild.get_role(cfg.get("creator_role_id") or 0)
        role_str = role.mention if role else "*(not set)*"

        tier = "⭐ Premium" if cfg.get("is_premium") else "Free"

        embed = discord.Embed(title="⚙️  Soren Configuration", color=discord.Color.blurple())
        embed.add_field(name="Event Creator Role", value=role_str, inline=False)
        embed.add_field(name="Plan",               value=tier,     inline=False)
        embed.add_field(
            name="Event Limit",
            value="Unlimited" if cfg.get("is_premium") else "5",
            inline=True,
        )
        embed.add_field(
            name="RSVP Limit per Event",
            value="Unlimited" if cfg.get("is_premium") else "50",
            inline=True,
        )
        gcal = "✅ Connected" if cfg.get("gcal_id") else "❌ Not connected"
        embed.add_field(name="Google Calendar", value=gcal, inline=False)

        await ctx.respond(embed=embed, ephemeral=True)

    # ── /setpremium (bot owner only) ──────────────────────────────────────
    @discord.slash_command(
        name="setpremium",
        description="[Bot Owner] Toggle premium status for a server."
    )
    async def setpremium(
        self,
        ctx: discord.ApplicationContext,
        enabled: discord.Option(bool, description="True to enable premium, False to disable."),
    ):
        """
        Bot-owner-only command to flip premium status for a guild.
        This would eventually be replaced by a payment integration.
        """
        if ctx.author.id != BOT_OWNER_ID:
            await ctx.respond(
                embed=build_error_embed("Only the bot owner can use this command."),
                ephemeral=True,
            )
            return

        upsert_guild_config(ctx.guild.id, is_premium=int(enabled))
        status = "enabled" if enabled else "disabled"
        await ctx.respond(
            embed=build_success_embed(f"Premium has been **{status}** for **{ctx.guild.name}**."),
            ephemeral=True,
        )


def setup(bot: discord.Bot):
    bot.add_cog(Setup(bot))
