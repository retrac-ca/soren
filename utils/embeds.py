"""
utils/embeds.py
================
Functions that build the Discord Embed objects used to display events.
Keeping embed construction here means all event displays look consistent
and any style changes only need to happen in one place.
"""

import discord
from datetime import datetime
import pytz


# ── Color constants ──────────────────────────────────────────────────────────
COLOR_EVENT    = discord.Color.from_rgb(88, 101, 242)   # Default blurple
COLOR_SUCCESS  = discord.Color.green()
COLOR_ERROR    = discord.Color.red()
COLOR_WARNING  = discord.Color.orange()
COLOR_REMINDER = discord.Color.gold()

# Free tier color options (3 colors)
FREE_COLORS = {
    "Blue":   discord.Color.from_rgb(88,  101, 242),
    "Red":    discord.Color.from_rgb(237,  66,  69),
    "Green":  discord.Color.from_rgb(87,  242, 135),
}

# Premium color options (includes free 3 + 7 more = 10 total)
PREMIUM_COLORS = {
    **FREE_COLORS,
    "Gold":   discord.Color.from_rgb(255, 184,  28),
    "Purple": discord.Color.from_rgb(155,  89, 182),
    "Cyan":   discord.Color.from_rgb(26,  188, 156),
    "Orange": discord.Color.from_rgb(230, 126,  34),
    "Brown":  discord.Color.from_rgb(152, 101,  60),
    "Pink":   discord.Color.from_rgb(233,  30, 140),
    "Olive":  discord.Color.from_rgb(128, 128,   0),
}


def get_guild_color(embed_color_hex: str | None) -> discord.Color:
    """Convert a stored hex string to a discord.Color."""
    if not embed_color_hex:
        return COLOR_EVENT
    try:
        return discord.Color(int(embed_color_hex, 16))
    except (ValueError, TypeError):
        return COLOR_EVENT


def build_event_embed(event: dict, rsvps: dict) -> discord.Embed:
    """
    Build the main event embed that gets posted in the channel.

    Parameters
    ----------
    event : dict
        A row from the events table (use dict(row) after fetching).
    rsvps : dict
        Keys: 'accepted', 'declined', 'tentative' — each a list of display names.

    Returns
    -------
    discord.Embed
    """
    # ── Format the start time with timezone ─────────────────────────────
    tz_name = event.get("timezone", "UTC")
    try:
        tz = pytz.timezone(tz_name)
        start_dt = datetime.fromisoformat(event["start_time"]).astimezone(tz)
        time_str = start_dt.strftime("%A, %B %d %Y  •  %I:%M %p %Z")
    except Exception:
        time_str = event["start_time"]  # Fallback to raw string

    # ── End time (optional) ──────────────────────────────────────────────
    end_str = ""
    if event.get("end_time"):
        try:
            end_dt = datetime.fromisoformat(event["end_time"]).astimezone(tz)
            end_str = f" → {end_dt.strftime('%I:%M %p %Z')}"
        except Exception:
            end_str = f" → {event['end_time']}"

    # ── Recurring label ──────────────────────────────────────────────────
    recur_label = ""
    if event.get("is_recurring"):
        rule = event.get("recur_rule", "recurring")
        recur_label = f"🔁 **Recurring** ({rule})\n"

    # ── RSVP cutoff line ─────────────────────────────────────────────────
    cutoff_str = ""
    if event.get("rsvp_cutoff"):
        try:
            tz      = pytz.timezone(tz_name)
            cut_dt  = datetime.fromisoformat(event["rsvp_cutoff"]).astimezone(tz)
            now_utc = datetime.now(pytz.utc)
            cutoff_dt_utc = datetime.fromisoformat(event["rsvp_cutoff"])
            if cutoff_dt_utc.tzinfo is None:
                cutoff_dt_utc = cutoff_dt_utc.replace(tzinfo=pytz.utc)
            if now_utc > cutoff_dt_utc.astimezone(pytz.utc):
                cutoff_str = f"\n🔒 **Signups closed**"
            else:
                cutoff_str = f"\n🔒 Signups close: **{cut_dt.strftime('%b %d  •  %I:%M %p %Z')}**"
        except Exception:
            pass

    # ── Build embed ──────────────────────────────────────────────────────
    guild_color = get_guild_color(event.get("embed_color"))
    embed = discord.Embed(
        title=f"📅  {event['title']}",
        description=(
            f"{recur_label}"
            f"🕐 **{time_str}{end_str}**"
            f"{cutoff_str}\n\n"
            f"{event.get('description', '')}"
        ),
        color=guild_color,
    )

    # ── RSVP fields ──────────────────────────────────────────────────────
    accepted  = rsvps.get("accepted",  [])
    declined  = rsvps.get("declined",  [])
    tentative = rsvps.get("tentative", [])

    def names_or_empty(names: list) -> str:
        return "\n".join(names) if names else "*None yet*"

    # Use custom button labels as field headers if available
    accept_label    = event.get("btn_accept_label")    or "✅ Accepted"
    tentative_label = event.get("btn_tentative_label") or "❓ Tentative"
    decline_label   = event.get("btn_decline_label")   or "❌ Declined"
    show_tentative  = bool(event.get("btn_tentative_enabled", 1))

    embed.add_field(
        name=f"{accept_label} ({len(accepted)})",
        value=names_or_empty(accepted),
        inline=True,
    )
    if show_tentative:
        embed.add_field(
            name=f"{tentative_label} ({len(tentative)})",
            value=names_or_empty(tentative),
            inline=True,
        )
    embed.add_field(
        name=f"{decline_label} ({len(declined)})",
        value=names_or_empty(declined),
        inline=True,
    )

    embed.set_footer(text=f"Event ID: {event['id']}  •  Use the buttons below to RSVP")
    return embed


def build_reminder_embed(event: dict) -> discord.Embed:
    """Compact embed used for pre-event reminder messages."""
    embed = discord.Embed(
        title=f"⏰  Reminder: {event['title']} is starting soon!",
        description=(
            f"The event **{event['title']}** starts in "
            f"**{event.get('reminder_offset', 15)} minutes**.\n\n"
            "Check the original event post for details."
        ),
        color=COLOR_REMINDER,
    )
    return embed


def build_error_embed(message: str) -> discord.Embed:
    """Generic error embed."""
    return discord.Embed(description=f"❌  {message}", color=COLOR_ERROR)


def build_success_embed(message: str) -> discord.Embed:
    """Generic success embed."""
    return discord.Embed(description=f"✅  {message}", color=COLOR_SUCCESS)