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

After a recurring event fires its reminder, the start_time (and end_time
if set) is advanced to the next occurrence and a fresh embed is posted.
Auto-spawning is a Premium-only feature — free servers still receive
reminders but their recurring events do not auto-advance.
"""

import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone
import pytz
import json
import logging

from utils.database import get_connection, is_premium
from utils.embeds import build_reminder_embed
from cogs.events import compute_next_start, repost_recurring_embed

log = logging.getLogger("soren.reminders")


def _try_refresh_token(token_json: str, creds_file: str) -> str | None:
    """
    Attempt to refresh an expired Google OAuth token.
    Returns updated token JSON string on success, None on failure.
    """
    try:
        import google.auth.transport.requests
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_info(json.loads(token_json))
        if creds.expired and creds.refresh_token:
            request = google.auth.transport.requests.Request()
            creds.refresh(request)
            log.info("OAuth token refreshed successfully.")
            return creds.to_json()
        return token_json  # Not expired, return as-is
    except Exception as e:
        log.error(f"Failed to refresh OAuth token: {e}")
        return None


class Reminders(commands.Cog):
    """Background loop that sends event reminders."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.reminder_loop.start()
        self.token_refresh_loop.start()

    def cog_unload(self):
        self.reminder_loop.cancel()
        self.token_refresh_loop.cancel()

    # ── Reminder loop ─────────────────────────────────────────────────────
    @tasks.loop(minutes=1)
    async def reminder_loop(self):
        """
        Runs every 60 seconds.
        Finds events whose reminder window has arrived and haven't
        been reminded yet (reminded_at IS NULL in DB).
        Uses a 5-minute lookback window so missed reminders still fire
        if the bot was briefly offline.
        """
        now_utc     = datetime.now(timezone.utc).replace(tzinfo=pytz.utc)
        now_minus_5 = now_utc - timedelta(minutes=5)
        now_plus_2h = now_utc + timedelta(hours=2)

        with get_connection() as conn:
            events = conn.execute(
                "SELECT * FROM events WHERE reminded_at IS NULL AND start_time BETWEEN ? AND ?",
                (now_minus_5.isoformat(), now_plus_2h.isoformat()),
            ).fetchall()

        for row in events:
            event    = dict(row)
            event_id = event["id"]

            try:
                start_dt = datetime.fromisoformat(event["start_time"])
                if start_dt.tzinfo is None:
                    start_dt = pytz.utc.localize(start_dt)
            except ValueError:
                continue

            offset_minutes = event.get("reminder_offset") or 15
            remind_at      = start_dt - timedelta(minutes=offset_minutes)

            diff = (now_utc - remind_at).total_seconds()
            if not (-60 <= diff <= 300):
                continue

            # Mark as reminded immediately to prevent double-sends
            with get_connection() as conn:
                conn.execute(
                    "UPDATE events SET reminded_at=? WHERE id=?",
                    (now_utc.isoformat(), event_id),
                )
                conn.commit()

            log.info(f"Sending reminder for event {event_id}: {event['title']}")
            await self._send_reminder(event)

            # ── Recurring auto-spawn (Premium only) ───────────────────────
            if event.get("is_recurring") and event.get("recur_rule"):
                if not is_premium(event["guild_id"]):
                    log.info(
                        f"Skipping auto-spawn for recurring event {event_id} "
                        f"(guild {event['guild_id']} is not Premium)"
                    )
                    continue

                next_start = compute_next_start(
                    event["start_time"], event["recur_rule"], event.get("recur_interval", 1)
                )
                if next_start:
                    # Also advance end_time by the same delta if one is set
                    next_end = None
                    if event.get("end_time"):
                        try:
                            start_dt_naive = datetime.fromisoformat(event["start_time"])
                            end_dt_naive   = datetime.fromisoformat(event["end_time"])
                            duration       = end_dt_naive - start_dt_naive
                            next_end       = (datetime.fromisoformat(next_start) + duration).isoformat()
                        except Exception:
                            next_end = None

                    with get_connection() as conn:
                        conn.execute(
                            "UPDATE events SET start_time=?, end_time=?, reminded_at=NULL WHERE id=?",
                            (next_start, next_end, event_id),
                        )
                        conn.commit()

                    log.info(
                        f"Auto-spawned next occurrence of recurring event {event_id} "
                        f"→ {next_start} (guild {event['guild_id']})"
                    )
                    await repost_recurring_embed(self.bot, event_id)
                else:
                    log.info(f"No more occurrences for recurring event {event_id}")

    @reminder_loop.before_loop
    async def before_reminder_loop(self):
        await self.bot.wait_until_ready()

    async def _send_reminder(self, event: dict):
        """Build and send the reminder message for a single event."""
        channel = self.bot.get_channel(event["channel_id"])
        if not channel:
            log.warning(f"Reminder: channel {event['channel_id']} not found for event {event['id']}")
            return

        embed      = build_reminder_embed(event)
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
            log.warning(
                f"Reminder: no permission to send in channel {channel.id} "
                f"(guild {channel.guild.id}) for event {event['id']}"
            )
        except Exception as e:
            log.error(f"Reminder: unexpected error sending for event {event['id']}: {e}")

    # ── OAuth token refresh loop ──────────────────────────────────────────
    @tasks.loop(hours=1)
    async def token_refresh_loop(self):
        """
        Runs every hour. Proactively refreshes Google OAuth tokens for
        both guild_config (primary /gcal sync) and gcal_integrations
        before they expire, preventing silent failures on the next post.
        """
        import os
        creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")

        # ── Refresh primary gcal tokens ───────────────────────────────────
        with get_connection() as conn:
            guilds = conn.execute(
                "SELECT guild_id, gcal_token FROM guild_config WHERE gcal_token IS NOT NULL"
            ).fetchall()

        for row in guilds:
            new_token = _try_refresh_token(row["gcal_token"], creds_file)
            if new_token and new_token != row["gcal_token"]:
                with get_connection() as conn:
                    conn.execute(
                        "UPDATE guild_config SET gcal_token=? WHERE guild_id=?",
                        (new_token, row["guild_id"]),
                    )
                    conn.commit()
                log.info(f"Refreshed primary gcal token for guild {row['guild_id']}")
            elif new_token is None:
                log.warning(f"Could not refresh primary gcal token for guild {row['guild_id']} — integration may fail")

        # ── Refresh gcal_integrations tokens ──────────────────────────────
        with get_connection() as conn:
            integrations = conn.execute(
                "SELECT id, guild_id, gcal_token FROM gcal_integrations WHERE active=1"
            ).fetchall()

        for row in integrations:
            new_token = _try_refresh_token(row["gcal_token"], creds_file)
            if new_token and new_token != row["gcal_token"]:
                with get_connection() as conn:
                    conn.execute(
                        "UPDATE gcal_integrations SET gcal_token=? WHERE id=?",
                        (new_token, row["id"]),
                    )
                    conn.commit()
                log.info(f"Refreshed gcal_integrations token for integration {row['id']} (guild {row['guild_id']})")
            elif new_token is None:
                log.warning(f"Could not refresh token for integration {row['id']} (guild {row['guild_id']}) — posts may fail")

    @token_refresh_loop.before_loop
    async def before_token_refresh_loop(self):
        await self.bot.wait_until_ready()


def setup(bot: discord.Bot):
    bot.add_cog(Reminders(bot))