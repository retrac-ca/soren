"""
cogs/modlogs.py
================
Per-server moderation logging system.

Admins choose a channel to receive structured log embeds whenever
notable things happen in Soren — events created/edited/deleted,
RSVPs, premium redemptions, setup changes, etc.

Commands (admin only)
---------------------
/modlogs setchannel #channel  — Set log channel and enable logging
/modlogs disable              — Pause logging (keeps channel saved)
/modlogs resume               — Resume logging
/modlogs status               — Show current config
"""

import discord
from discord.ext import commands
from datetime import datetime, timezone
import logging

from utils.database import get_connection, upsert_guild_config, get_guild_config
from utils.embeds import build_error_embed, build_success_embed

log = logging.getLogger("soren.modlogs")


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_modlogs_config(guild_id: int) -> dict | None:
    """Return the modlogs config row for a guild, or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM modlogs_config WHERE guild_id=?", (guild_id,)
        ).fetchone()
    return dict(row) if row else None


def set_modlogs_config(guild_id: int, channel_id: int | None = None,
                       enabled: int | None = None):
    """Upsert modlogs config for a guild."""
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT * FROM modlogs_config WHERE guild_id=?", (guild_id,)
        ).fetchone()

        if existing:
            updates = {}
            if channel_id is not None:
                updates["channel_id"] = channel_id
            if enabled is not None:
                updates["enabled"] = enabled
            if updates:
                sets = ", ".join(f"{k}=?" for k in updates)
                vals = list(updates.values()) + [guild_id]
                conn.execute(f"UPDATE modlogs_config SET {sets} WHERE guild_id=?", vals)
        else:
            conn.execute(
                "INSERT INTO modlogs_config (guild_id, channel_id, enabled) VALUES (?, ?, ?)",
                (guild_id, channel_id or 0, enabled if enabled is not None else 1),
            )
        conn.commit()


# ── Core log dispatcher ───────────────────────────────────────────────────────

async def log_event(bot: discord.Bot, guild_id: int, embed: discord.Embed):
    """
    Send a log embed to the guild's configured modlog channel.
    Silently does nothing if modlogs are not configured or disabled.
    """
    cfg = get_modlogs_config(guild_id)
    if not cfg or not cfg.get("enabled") or not cfg.get("channel_id"):
        return

    channel = bot.get_channel(cfg["channel_id"])
    if not channel:
        log.warning(f"modlogs: channel {cfg['channel_id']} not found for guild {guild_id}")
        return

    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        log.warning(f"modlogs: no permission to send in channel {cfg['channel_id']} (guild {guild_id})")
    except Exception as e:
        log.error(f"modlogs: unexpected error posting to guild {guild_id}: {e}")


# ── Embed builders ────────────────────────────────────────────────────────────

def _base_embed(title: str, color: discord.Color) -> discord.Embed:
    embed = discord.Embed(title=title, color=color,
                          timestamp=datetime.now(timezone.utc))
    return embed


def embed_event_created(event: dict, creator: discord.Member) -> discord.Embed:
    embed = _base_embed("📅  Event Created", discord.Color.green())
    embed.add_field(name="Title",    value=event.get("title", "Unknown"), inline=True)
    embed.add_field(name="Event ID", value=f"`{event.get('id')}`",        inline=True)
    embed.add_field(name="Channel",  value=f"<#{event.get('channel_id')}>", inline=True)
    recur = event.get("recur_rule") or "none"
    embed.add_field(name="Recurrence", value=recur.capitalize(), inline=True)
    embed.add_field(name="Start",    value=event.get("start_time", "?"), inline=True)
    embed.set_footer(text=f"Created by {creator.display_name}", icon_url=creator.display_avatar.url)
    return embed


def embed_event_deleted(event: dict, actor: discord.Member) -> discord.Embed:
    embed = _base_embed("🗑️  Event Deleted", discord.Color.red())
    embed.add_field(name="Title",    value=event.get("title", "Unknown"), inline=True)
    embed.add_field(name="Event ID", value=f"`{event.get('id')}`",        inline=True)
    embed.set_footer(text=f"Deleted by {actor.display_name}", icon_url=actor.display_avatar.url)
    return embed


def embed_event_cancelled(event: dict, actor: discord.Member) -> discord.Embed:
    embed = _base_embed("🚫  Event Cancelled", discord.Color.orange())
    embed.add_field(name="Title",    value=event.get("title", "Unknown"), inline=True)
    embed.add_field(name="Event ID", value=f"`{event.get('id')}`",        inline=True)
    embed.set_footer(text=f"Cancelled by {actor.display_name}", icon_url=actor.display_avatar.url)
    return embed


def embed_event_edited(event: dict, actor: discord.Member, what: str) -> discord.Embed:
    embed = _base_embed("✏️  Event Edited", discord.Color.blurple())
    embed.add_field(name="Title",    value=event.get("title", "Unknown"), inline=True)
    embed.add_field(name="Event ID", value=f"`{event.get('id')}`",        inline=True)
    embed.add_field(name="Changed",  value=what,                          inline=False)
    embed.set_footer(text=f"Edited by {actor.display_name}", icon_url=actor.display_avatar.url)
    return embed


def embed_rsvp(event_title: str, event_id: int,
               member: discord.Member, status: str) -> discord.Embed:
    status_map = {
        "accepted":  ("✅", "Accepted",  discord.Color.green()),
        "tentative": ("❓", "Tentative", discord.Color.gold()),
        "declined":  ("❌", "Declined",  discord.Color.red()),
        "removed":   ("➖", "Removed",   discord.Color.light_grey()),
    }
    emoji, label, color = status_map.get(status, ("❓", status, discord.Color.blurple()))
    embed = _base_embed(f"{emoji}  RSVP Update", color)
    embed.add_field(name="Member",   value=member.mention,       inline=True)
    embed.add_field(name="Event",    value=event_title,          inline=True)
    embed.add_field(name="Event ID", value=f"`{event_id}`",      inline=True)
    embed.add_field(name="Status",   value=label,                inline=True)
    embed.set_footer(text=member.display_name, icon_url=member.display_avatar.url)
    return embed


def embed_premium_redeemed(actor: discord.Member, code: str) -> discord.Embed:
    embed = _base_embed("⭐  Premium Activated", discord.Color.gold())
    embed.add_field(name="Redeemed by", value=actor.mention, inline=True)
    embed.add_field(name="Code",        value=f"`{code}`",   inline=True)
    embed.set_footer(text=actor.display_name, icon_url=actor.display_avatar.url)
    return embed


def embed_setup_changed(old_role: str, new_role: str, actor: discord.Member) -> discord.Embed:
    embed = _base_embed("⚙️  Setup Changed", discord.Color.blurple())
    embed.add_field(name="Previous Role", value=old_role, inline=True)
    embed.add_field(name="New Role",      value=new_role, inline=True)
    embed.set_footer(text=f"Changed by {actor.display_name}", icon_url=actor.display_avatar.url)
    return embed


def embed_color_changed(old_color: str, new_color: str, actor: discord.Member) -> discord.Embed:
    embed = _base_embed("🎨  Embed Color Changed", discord.Color.blurple())
    embed.add_field(name="Previous", value=old_color or "Default", inline=True)
    embed.add_field(name="New",      value=new_color,              inline=True)
    embed.set_footer(text=f"Changed by {actor.display_name}", icon_url=actor.display_avatar.url)
    return embed


# ── Cog ───────────────────────────────────────────────────────────────────────

class ModLogs(commands.Cog):
    """Per-server moderation logging."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    modlogs = discord.SlashCommandGroup(
        "modlogs",
        "Configure server activity logging.",
        default_member_permissions=discord.Permissions(administrator=True),
    )

    # ── /modlogs setchannel ───────────────────────────────────────────────
    @modlogs.command(name="setchannel", description="Set the channel for Soren activity logs.")
    async def modlogs_setchannel(
        self,
        ctx: discord.ApplicationContext,
        channel: discord.Option(discord.TextChannel, "Channel to post logs in.", required=True),
    ):
        set_modlogs_config(ctx.guild.id, channel_id=channel.id, enabled=1)
        log.info(f"modlogs: guild {ctx.guild.id} set log channel to {channel.id}")

        embed = build_success_embed(
            f"Activity logs will now be posted in {channel.mention}.\n"
            "Use `/modlogs disable` to pause at any time."
        )
        await ctx.respond(embed=embed, ephemeral=True)

    # ── /modlogs disable ──────────────────────────────────────────────────
    @modlogs.command(name="disable", description="Pause activity logging (keeps channel saved).")
    async def modlogs_disable(self, ctx: discord.ApplicationContext):
        cfg = get_modlogs_config(ctx.guild.id)
        if not cfg:
            await ctx.respond(
                embed=build_error_embed("Modlogs aren't configured yet. Run `/modlogs setchannel` first."),
                ephemeral=True,
            )
            return
        if not cfg.get("enabled"):
            await ctx.respond(
                embed=build_error_embed("Modlogs are already disabled."),
                ephemeral=True,
            )
            return

        set_modlogs_config(ctx.guild.id, enabled=0)
        log.info(f"modlogs: guild {ctx.guild.id} disabled modlogs")
        await ctx.respond(
            embed=build_success_embed("Activity logging paused. Run `/modlogs resume` to re-enable."),
            ephemeral=True,
        )

    # ── /modlogs resume ───────────────────────────────────────────────────
    @modlogs.command(name="resume", description="Resume activity logging.")
    async def modlogs_resume(self, ctx: discord.ApplicationContext):
        cfg = get_modlogs_config(ctx.guild.id)
        if not cfg:
            await ctx.respond(
                embed=build_error_embed("Modlogs aren't configured yet. Run `/modlogs setchannel` first."),
                ephemeral=True,
            )
            return
        if cfg.get("enabled"):
            await ctx.respond(
                embed=build_error_embed("Modlogs are already enabled."),
                ephemeral=True,
            )
            return

        set_modlogs_config(ctx.guild.id, enabled=1)
        log.info(f"modlogs: guild {ctx.guild.id} resumed modlogs")
        channel = self.bot.get_channel(cfg["channel_id"])
        ch_str  = channel.mention if channel else f"<#{cfg['channel_id']}>"
        await ctx.respond(
            embed=build_success_embed(f"Activity logging resumed. Logs will post in {ch_str}."),
            ephemeral=True,
        )

    # ── /modlogs status ───────────────────────────────────────────────────
    @modlogs.command(name="status", description="Show current modlogs configuration.")
    async def modlogs_status(self, ctx: discord.ApplicationContext):
        cfg = get_modlogs_config(ctx.guild.id)
        if not cfg or not cfg.get("channel_id"):
            await ctx.respond(
                embed=build_error_embed("Modlogs are not configured. Run `/modlogs setchannel` to set up."),
                ephemeral=True,
            )
            return

        channel  = self.bot.get_channel(cfg["channel_id"])
        ch_str   = channel.mention if channel else f"<#{cfg['channel_id']}> *(channel not found)*"
        status   = "✅ Enabled" if cfg.get("enabled") else "⏸️ Disabled"

        embed = discord.Embed(title="📋  Modlogs Status", color=discord.Color.blurple())
        embed.add_field(name="Log Channel", value=ch_str, inline=False)
        embed.add_field(name="Status",      value=status, inline=True)
        embed.add_field(
            name="Events Logged",
            value=(
                "Event created/deleted/cancelled/edited\n"
                "RSVP changes\n"
                "Premium redemptions\n"
                "Setup & embed color changes"
            ),
            inline=False,
        )
        await ctx.respond(embed=embed, ephemeral=True)


# ── Setup ──────────────────────────────────────────────────────────────────────

def setup(bot: discord.Bot):
    bot.add_cog(ModLogs(bot))
    log.info("ModLogs cog loaded.")