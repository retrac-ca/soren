"""
cogs/reminders.py
==================
Background task that runs every minute, checks for upcoming events,
and sends reminder messages to the channel where the event was created.

Reminders go out `reminder_offset` minutes before the event starts
(default 15 minutes). They ping:
  - The notify_role (if configured on the event), AND
  - All users whose RSVP status is 'accepted' or 'tentative'.

Reminders are tracked in the DB (reminded_at column) so they survive
bot restarts and are never sent twice.
"""

import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import pytz
import logging

from utils.database import get_connection
from utils.embeds import build_reminder_embed

log = logging.getLogger("soren.reminders")


class Reminders(commands.Cog):
    """Background loop that sends event reminders."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.reminder_loop.start()

    def cog_unload(self):
        self.reminder_loop.cancel()

    @tasks.loop(minutes=1)
    async def reminder_loop(self):
        """
        Runs every 60 seconds.
        Finds events whose reminder window has arrived and haven't
        been reminded yet (reminded_at IS NULL in DB).
        Uses a 5-minute lookback window so missed reminders still fire
        if the bot was briefly offline.
        """
        now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)

        with get_connection() as conn:
            # Only fetch events that haven't been reminded yet
            events = conn.execute(
                "SELECT * FROM events WHERE reminded_at IS NULL"
            ).fetchall()

        for row in events:
            event = dict(row)
            event_id = event["id"]

            try:
                start_dt = datetime.fromisoformat(event["start_time"])
                if start_dt.tzinfo is None:
                    start_dt = pytz.utc.localize(start_dt)
            except ValueError:
                continue

            offset_minutes = event.get("reminder_offset") or 15
            remind_at = start_dt - timedelta(minutes=offset_minutes)

            # Fire if we're within a 5-minute window of the reminder time
            # (5 min lookback handles brief bot downtime / missed 1-min ticks)
            diff = (now_utc - remind_at).total_seconds()
            if not (-60 <= diff <= 300):
                continue  # Not time yet, or more than 5 minutes past

            # Mark as reminded in DB immediately to prevent double-sends
            with get_connection() as conn:
                conn.execute(
                    "UPDATE events SET reminded_at=? WHERE id=?",
                    (now_utc.isoformat(), event_id),
                )
                conn.commit()

            log.info(f"Sending reminder for event {event_id}: {event['title']}")
            await self._send_reminder(event)

    @reminder_loop.before_loop
    async def before_reminder_loop(self):
        await self.bot.wait_until_ready()

    async def _send_reminder(self, event: dict):
        """Build and send the reminder message for a single event."""
        channel = self.bot.get_channel(event["channel_id"])
        if not channel:
            log.warning(f"Reminder: channel {event['channel_id']} not found for event {event['id']}")
            return

        embed = build_reminder_embed(event)

        ping_parts = []

        if event.get("notify_role_id"):
            role = channel.guild.get_role(event["notify_role_id"])
            if role:
                ping_parts.append(role.mention)
            else:
                log.warning(f"Reminder: role {event['notify_role_id']} not found in guild")

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
            log.info(f"Reminder sent for event {event['id']} ({event['title']})")
        except discord.Forbidden:
            log.warning(f"No permission to send reminder in channel {channel.id}")


def setup(bot: discord.Bot):
    bot.add_cog(Reminders(bot))