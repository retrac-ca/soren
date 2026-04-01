"""
cogs/setup.py
==============
Handles server setup, config viewing, and embed color customization.

Commands
--------
/setup        — Assign Event Creator role (admin only)
/config       — View current server settings
/embedcolor   — Choose the event embed color (free: 3 options, premium: 8)
"""

import discord
from discord.ext import commands
from utils.database import upsert_guild_config, get_guild_config, get_connection
from utils.embeds import (
    build_error_embed, build_success_embed,
    FREE_COLORS, PREMIUM_COLORS, get_guild_color
)
from utils.database import is_premium
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
]


class ColorSelectView(discord.ui.View):
    """Dropdown for choosing embed color — options depend on premium status."""

    def __init__(self, author_id: int, guild_id: int, premium: bool):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.guild_id  = guild_id
        options = PREMIUM_COLOR_OPTIONS if premium else FREE_COLOR_OPTIONS

        select = discord.ui.Select(
            placeholder="Choose an embed color...",
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran this command can use this menu.",
                ephemeral=True,
            )
            return

        hex_value = interaction.data["values"][0]
        # Find the label for display
        all_opts = PREMIUM_COLOR_OPTIONS
        label = next((o.label for o in all_opts if o.value == hex_value), hex_value)

        upsert_guild_config(self.guild_id, embed_color=hex_value)
        log.info(f"Embed color set to {label} (#{hex_value}) for guild {self.guild_id}")

        color = get_guild_color(hex_value)
        embed = discord.Embed(
            title="🎨  Embed Color Updated",
            description=(
                f"Event embeds will now use **{label}** as their color.\n\n"
                "This preview embed shows the new color."
            ),
            color=color,
        )
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)


class Setup(commands.Cog):
    """Server setup and configuration commands."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    # ── /setup ────────────────────────────────────────────────────────────
    @discord.slash_command(
        name="setup",
        description="Configure Soren for your server. (Admin only)"
    )
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
        upsert_guild_config(ctx.guild.id, creator_role_id=event_role.id)
        log.info(f"Setup completed for guild {ctx.guild.id} ({ctx.guild.name})")

        embed = discord.Embed(
            title="✅  Soren is ready!",
            description=(
                f"**Event Creator Role:** {event_role.mention}\n\n"
                "Members with this role (plus server admins) can now:\n"
                "• Create events with `/newevent`\n"
                "• Edit events with `/editevent`\n"
                "• Delete events with `/deleteevent`\n\n"
                "Everyone else can RSVP to events using the buttons on event posts.\n\n"
                "Use `/embedcolor` to customize the color of event embeds.\n"
                "Run `/config` any time to review your settings."
            ),
            color=discord.Color.green(),
        )
        await ctx.respond(embed=embed)

    # ── /embedcolor ───────────────────────────────────────────────────────
    @discord.slash_command(
        name="embedcolor",
        description="Choose the color for event embeds on this server."
    )
    @discord.default_permissions(administrator=True)
    async def embedcolor(self, ctx: discord.ApplicationContext):
        """
        Shows a color picker dropdown. Free servers get 3 colors,
        Premium servers get 8 colors.
        """
        premium = is_premium(ctx.guild.id)
        color_count = len(PREMIUM_COLOR_OPTIONS) if premium else len(FREE_COLOR_OPTIONS)
        tier_note = "⭐ Premium — all 8 colors available" if premium else "Free — upgrade to Premium for 5 more colors"

        embed = discord.Embed(
            title="🎨  Choose Embed Color",
            description=(
                f"{tier_note}\n\n"
                f"**{color_count} colors available.** Pick one below:"
            ),
            color=discord.Color.blurple(),
        )
        view = ColorSelectView(
            author_id=ctx.author.id,
            guild_id=ctx.guild.id,
            premium=premium,
        )
        await ctx.respond(embed=embed, view=view, ephemeral=True)

    # ── /config ───────────────────────────────────────────────────────────
    @discord.slash_command(
        name="config",
        description="View Soren's current configuration for this server."
    )
    @discord.default_permissions(administrator=True)
    async def config(self, ctx: discord.ApplicationContext):
        cfg = get_guild_config(ctx.guild.id)

        if not cfg:
            await ctx.respond(
                embed=build_error_embed("Soren hasn't been set up yet. Run `/setup` first."),
                ephemeral=True,
            )
            return

        role = ctx.guild.get_role(cfg.get("creator_role_id") or 0)
        role_str = role.mention if role else "*(not set)*"
        tier = "⭐ Premium" if cfg.get("is_premium") else "Free"

        # Resolve color name from hex
        hex_val = cfg.get("embed_color") or "5865F2"
        color_name = next(
            (o.label for o in PREMIUM_COLOR_OPTIONS if o.value == hex_val),
            f"#{hex_val}"
        )

        embed = discord.Embed(
            title="⚙️  Soren Configuration",
            color=get_guild_color(hex_val)
        )
        embed.add_field(name="Event Creator Role", value=role_str,    inline=False)
        embed.add_field(name="Plan",               value=tier,        inline=True)
        embed.add_field(name="Embed Color",        value=color_name,  inline=True)
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


def setup(bot: discord.Bot):
    bot.add_cog(Setup(bot))