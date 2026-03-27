"""
cogs/reminders.py
==================
Background task that runs every minute, checks for upcoming events,
and sends reminder messages to the channel where the event was created.

Reminders go out `reminder_offset` minutes before the event starts
(default 15 minutes). They ping:
  - The notify_role (if configured on the event), AND
  - All users whose RSVP status is 'accepted' or 'tentative'.
"""

import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import pytz
import logging

from utils.database import get_connection
from utils.embeds import build_reminder_embed

log = logging.getLogger("soren.reminders")

# Track which event IDs we've already reminded about in this session
# (prevents duplicate reminders if the loop runs while a reminder is "active")
_reminded_this_session: set[int] = set()


class Reminders(commands.Cog):
    """Background loop that sends event reminders."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.reminder_loop.start()   # Start the loop when the cog loads

    def cog_unload(self):
        """Stop the loop cleanly when the cog is unloaded."""
        self.reminder_loop.cancel()

    @tasks.loop(minutes=1)
    async def reminder_loop(self):
        """
        Runs every 60 seconds.
        Finds events whose start time is within the next `reminder_offset`
        minutes and sends a reminder if one hasn't been sent yet.
        """
        now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)

        with get_connection() as conn:
            events = conn.execute("SELECT * FROM events").fetchall()

        for row in events:
            event = dict(row)
            event_id = event["id"]

            if event_id in _reminded_this_session:
                continue  # Already reminded in this run

            # Parse start time
            try:
                start_dt = datetime.fromisoformat(event["start_time"])
                if start_dt.tzinfo is None:
                    start_dt = pytz.utc.localize(start_dt)
            except ValueError:
                continue  # Bad timestamp — skip

            offset_minutes = event.get("reminder_offset") or 15
            remind_at = start_dt - timedelta(minutes=offset_minutes)

            # Check if we're within a 1-minute window of the reminder time
            diff = (remind_at - now_utc).total_seconds()
            if not (0 <= diff <= 60):
                continue  # Not time yet (or already past)

            # Mark as reminded so we don't double-send within the same window
            _reminded_this_session.add(event_id)
            log.info(f"Sending reminder for event {event_id}: {event['title']}")

            await self._send_reminder(event)

    @reminder_loop.before_loop
    async def before_reminder_loop(self):
        """Wait until the bot is fully connected before starting the loop."""
        await self.bot.wait_until_ready()

    async def _send_reminder(self, event: dict):
        """Build and send the reminder message for a single event."""
        channel = self.bot.get_channel(event["channel_id"])
        if not channel:
            return

        # Build the reminder embed
        embed = build_reminder_embed(event)

        # Collect users to ping: role + accepted/tentative RSVPs
        ping_parts = []

        # Role ping (if set on the event)
        if event.get("notify_role_id"):
            role = channel.guild.get_role(event["notify_role_id"])
            if role:
                ping_parts.append(role.mention)

        # Individual pings for accepted/tentative RSVPers
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT user_id FROM rsvps WHERE event_id=? AND status IN ('accepted','tentative')",
                (event["id"],),
            ).fetchall()

        for row in rows:
            member = channel.guild.get_member(row["user_id"])
            if member:
                ping_parts.append(member.mention)

        ping_str = " ".join(ping_parts) if ping_parts else ""

        try:
            await channel.send(content=ping_str or None, embed=embed)
        except discord.Forbidden:
            log.warning(f"No permission to send reminder in channel {channel.id}")


def setup(bot: discord.Bot):
    bot.add_cog(Reminders(bot))
