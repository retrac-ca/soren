"""
cogs/setup.py
==============
Handles server setup, config viewing, and embed color customization.

Commands
--------
/setup        — Assign Event Creator role (admin only); warns if already configured
/config       — View current server settings
/embedcolor   — Choose the event embed color (free: 3 options, premium: 10)
"""

import discord
from discord.ext import commands
from utils.database import upsert_guild_config, get_guild_config, get_connection
from utils.embeds import (
    build_error_embed, build_success_embed,
    FREE_COLORS, PREMIUM_COLORS, get_guild_color
)
from utils.database import is_premium
from cogs.events import FREE_EVENT_LIMIT, PREMIUM_EVENT_LIMIT
from cogs.rsvp import FREE_RSVP_DISPLAY_LIMIT, PREMIUM_RSVP_DISPLAY_LIMIT
import logging

log = logging.getLogger("soren.setup")

# ── Color select options ──────────────────────────────────────────────────────
FREE_COLOR_OPTIONS = [
    discord.SelectOption(label="Blue",   value="5865F2", emoji="🔵", description="Default blurple"),
    discord.SelectOption(label="Red",    value="ED4245", emoji="🔴"),
    discord.SelectOption(label="Green",  value="57F287", emoji="🟢"),
]

PREMIUM_COLOR_OPTIONS = FREE_COLOR_OPTIONS + [
    discord.SelectOption(label="Gold",   value="FFB81C", emoji="🟡"),
    discord.SelectOption(label="Purple", value="9B59B6", emoji="🟣"),
    discord.SelectOption(label="Cyan",   value="1ABC9C", emoji="🩵"),
    discord.SelectOption(label="Orange", value="E67E22", emoji="🟠"),
    discord.SelectOption(label="Brown",  value="98653C", emoji="🟤"),
    discord.SelectOption(label="Pink",   value="E91E8C", emoji="🩷"),
    discord.SelectOption(label="Olive",  value="808000", emoji="🫒"),
]


class ColorSelectView(discord.ui.View):
    """Dropdown for choosing embed color — options depend on premium status."""

    def __init__(self, author_id: int, guild_id: int, premium: bool):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.guild_id  = guild_id
        options = PREMIUM_COLOR_OPTIONS if premium else FREE_COLOR_OPTIONS

        select = discord.ui.Select(placeholder="Choose an embed color...", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran this command can use this menu.", ephemeral=True
            )
            return

        hex_value  = interaction.data["values"][0]
        label      = next((o.label for o in PREMIUM_COLOR_OPTIONS if o.value == hex_value), hex_value)
        upsert_guild_config(self.guild_id, embed_color=hex_value)
        log.info(f"Embed color set to {label} (#{hex_value}) for guild {self.guild_id}")

        color = get_guild_color(hex_value)
        embed = discord.Embed(
            title="🎨  Embed Color Updated",
            description=f"Event embeds will now use **{label}** as their color.\n\nThis preview embed shows the new color.",
            color=color,
        )
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)


class SetupConfirmView(discord.ui.View):
    """
    Shown when /setup is run on a server that's already configured.
    Requires explicit confirmation before overwriting the existing role.
    """

    def __init__(self, guild_id: int, new_role: discord.Role, author_id: int):
        super().__init__(timeout=30)
        self.guild_id  = guild_id
        self.new_role  = new_role
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the admin who ran `/setup` can confirm this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, update it", style=discord.ButtonStyle.danger)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.stop()
        upsert_guild_config(self.guild_id, creator_role_id=self.new_role.id)
        log.info(f"Setup updated for guild {self.guild_id} — new role: {self.new_role.id}")
        embed = discord.Embed(
            title="✅  Setup Updated",
            description=(
                f"**Event Creator Role** updated to {self.new_role.mention}.\n\n"
                "Run `/config` to confirm your current settings."
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(description="❎  Setup change cancelled. Existing configuration kept.", color=discord.Color.blurple()),
            view=None,
        )


class Setup(commands.Cog):
    """Server setup and configuration commands."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    # ── /setup ────────────────────────────────────────────────────────────
    @discord.slash_command(name="setup", description="Configure Soren for your server. (Admin only)")
    @discord.default_permissions(administrator=True)
    async def setup(
        self,
        ctx: discord.ApplicationContext,
        event_role: discord.Option(
            discord.Role,
            description="The role whose members can create/edit/delete events.",
            required=True,
        ),
    ):
        existing = get_guild_config(ctx.guild.id)

        if existing and existing.get("creator_role_id"):
            current_role = ctx.guild.get_role(existing["creator_role_id"])
            current_name = current_role.mention if current_role else f"ID {existing['creator_role_id']}"
            embed = discord.Embed(
                title="⚠️  Soren is Already Configured",
                description=(
                    f"This server already has Soren set up.\n\n"
                    f"**Current Event Creator Role:** {current_name}\n"
                    f"**New Role:** {event_role.mention}\n\n"
                    "Do you want to update the Event Creator role?"
                ),
                color=discord.Color.orange(),
            )
            await ctx.respond(
                embed=embed,
                view=SetupConfirmView(guild_id=ctx.guild.id, new_role=event_role, author_id=ctx.author.id),
                ephemeral=True,
            )
            return

        upsert_guild_config(ctx.guild.id, creator_role_id=event_role.id)
        log.info(f"Setup completed for guild {ctx.guild.id} ({ctx.guild.name})")

        embed = discord.Embed(
            title="✅  Soren is Ready!",
            description=(
                f"**Event Creator Role:** {event_role.mention}\n\n"
                "Members with this role (plus server admins) can now:\n"
                "• Create events with `/newevent`\n"
                "• Edit events with `/editeventdetails` and `/editeventtime`\n"
                "• Delete events with `/deleteevent`\n\n"
                "Everyone else can RSVP to events using the buttons on event posts.\n\n"
                "Use `/embedcolor` to customize the color of event embeds.\n"
                "Run `/config` any time to review your settings."
            ),
            color=discord.Color.green(),
        )
        await ctx.respond(embed=embed)

    # ── /embedcolor ───────────────────────────────────────────────────────
    @discord.slash_command(name="embedcolor", description="Choose the color for event embeds on this server.")
    @discord.default_permissions(administrator=True)
    async def embedcolor(self, ctx: discord.ApplicationContext):
        premium     = is_premium(ctx.guild.id)
        color_count = len(PREMIUM_COLOR_OPTIONS) if premium else len(FREE_COLOR_OPTIONS)
        tier_note   = (
            f"⭐ Premium — all {len(PREMIUM_COLOR_OPTIONS)} colors available"
            if premium else
            f"Free — upgrade to Premium for {len(PREMIUM_COLOR_OPTIONS) - len(FREE_COLOR_OPTIONS)} more colors"
        )

        embed = discord.Embed(
            title="🎨  Choose Embed Color",
            description=f"{tier_note}\n\n**{color_count} colors available.** Pick one below:",
            color=discord.Color.blurple(),
        )
        await ctx.respond(
            embed=embed,
            view=ColorSelectView(author_id=ctx.author.id, guild_id=ctx.guild.id, premium=premium),
            ephemeral=True,
        )

    # ── /config ───────────────────────────────────────────────────────────
    @discord.slash_command(name="config", description="View Soren's current configuration for this server.")
    @discord.default_permissions(administrator=True)
    async def config(self, ctx: discord.ApplicationContext):
        cfg = get_guild_config(ctx.guild.id)
        if not cfg:
            await ctx.respond(
                embed=build_error_embed("Soren hasn't been set up yet. Run `/setup` first."),
                ephemeral=True,
            )
            return

        premium    = bool(cfg.get("is_premium"))
        role       = ctx.guild.get_role(cfg.get("creator_role_id") or 0)
        role_str   = role.mention if role else "*(not set)*"
        tier       = "⭐ Premium" if premium else "Free"
        hex_val    = cfg.get("embed_color") or "5865F2"
        color_name = next((o.label for o in PREMIUM_COLOR_OPTIONS if o.value == hex_val), f"#{hex_val}")

        embed = discord.Embed(title="⚙️  Soren Configuration", color=get_guild_color(hex_val))
        embed.add_field(name="Event Creator Role", value=role_str,   inline=False)
        embed.add_field(name="Plan",               value=tier,       inline=True)
        embed.add_field(name="Embed Color",        value=color_name, inline=True)
        embed.add_field(
            name="Active Event Limit",
            value=str(PREMIUM_EVENT_LIMIT) if premium else str(FREE_EVENT_LIMIT),
            inline=True,
        )
        embed.add_field(
            name="RSVP Names Shown",
            value=str(PREMIUM_RSVP_DISPLAY_LIMIT) if premium else str(FREE_RSVP_DISPLAY_LIMIT),
            inline=True,
        )
        gcal = "✅ Connected" if cfg.get("gcal_id") else "❌ Not connected"
        embed.add_field(name="Google Calendar", value=gcal, inline=False)

        await ctx.respond(embed=embed, ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(Setup(bot))