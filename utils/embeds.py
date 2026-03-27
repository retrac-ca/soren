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
COLOR_EVENT    = discord.Color.from_rgb(88, 101, 242)   # Blurple
COLOR_SUCCESS  = discord.Color.green()
COLOR_ERROR    = discord.Color.red()
COLOR_WARNING  = discord.Color.orange()
COLOR_REMINDER = discord.Color.gold()


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

    # ── Build embed ──────────────────────────────────────────────────────
    embed = discord.Embed(
        title=f"📅  {event['title']}",
        description=(
            f"{recur_label}"
            f"🕐 **{time_str}{end_str}**\n\n"
            f"{event.get('description', '')}"
        ),
        color=COLOR_EVENT,
    )

    # ── RSVP fields ──────────────────────────────────────────────────────
    accepted  = rsvps.get("accepted",  [])
    declined  = rsvps.get("declined",  [])
    tentative = rsvps.get("tentative", [])

    def names_or_empty(names: list) -> str:
        return "\n".join(names) if names else "*None yet*"

    embed.add_field(
        name=f"✅ Accepted ({len(accepted)})",
        value=names_or_empty(accepted),
        inline=True,
    )
    embed.add_field(
        name=f"❓ Tentative ({len(tentative)})",
        value=names_or_empty(tentative),
        inline=True,
    )
    embed.add_field(
        name=f"❌ Declined ({len(declined)})",
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
