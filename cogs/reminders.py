"""
cogs/reminders.py
==================
Background task that runs every minute, checks for upcoming events,
and sends reminder messages to the channel where the event was created.

Reminders go out `reminder_offset` minutes before the event starts
(default 15 minutes). They ping:
  - All roles listed in notify_role_ids (JSON array), OR the legacy
    notify_role_id field for older events.
  - All users whose RSVP status is 'accepted' or 'tentative'.

Reminders are tracked in the DB (reminded_at column) so they survive
bot restarts and are never sent twice.

After a recurring event fires its reminder, the start_time is advanced
to the next occurrence and a fresh embed is posted in the channel.

IMPORTANT — timezone handling:
  Do NOT use SQL BETWEEN for ISO datetime comparison when events may be
  stored with mixed UTC offsets. SQLite compares them as raw strings,
  which gives wrong results for e.g. "2026-04-09T20:00:00-04:00".
  Instead, fetch all unreminded events within a generous lookahead
  window and do the precise comparison in Python after normalising to UTC.
"""

import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone
import pytz
import json
import logging

from utils.database import get_connection
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


def _parse_role_ids(event: dict) -> list[int]:
    """
    Return a list of notify role IDs for an event.
    Reads the new notify_role_ids JSON array first; falls back to the
    legacy notify_role_id integer column for older rows.
    """
    raw = event.get("notify_role_ids")
    if raw:
        try:
            ids = json.loads(raw)
            if isinstance(ids, list) and ids:
                return [int(i) for i in ids if i]
        except (json.JSONDecodeError, ValueError):
            pass
    # Legacy single-role fallback
    legacy = event.get("notify_role_id")
    if legacy:
        return [int(legacy)]
    return []


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

        Strategy:
          1. Fetch ALL unreminded, non-cancelled events from the DB.
          2. For each event, compute remind_at = start_time - reminder_offset.
          3. Quick skip: if remind_at is more than 2 hours away, ignore for now.
             NOTE: we skip based on remind_at (not start_time) so that long
             offsets (e.g. 1-day reminders) are caught at the right time, and
             so that events created close to their reminder window are not
             missed because start_time happens to fall just outside the
             now+2h lookahead.
          4. Fire if now is within the -60s / +300s window around remind_at.
             The 60s early tolerance covers loop jitter; the 300s lookback
             means reminders survive a bot restart of up to 5 minutes.
        """
        now_utc     = datetime.now(timezone.utc)
        now_plus_2h = now_utc + timedelta(hours=2)

        with get_connection() as conn:
            events = conn.execute(
                "SELECT * FROM events WHERE reminded_at IS NULL"
            ).fetchall()

        log.info(f"Reminder loop tick: {len(events)} unreminded event(s) found, now_utc={now_utc.isoformat()}")

        for row in events:
            event    = dict(row)
            event_id = event["id"]

            # Skip cancelled events — they should never send reminders
            if event.get("title", "").startswith("[CANCELLED]"):
                log.debug(f"Reminder: skipping cancelled event {event_id} '{event['title']}'")
                continue

            try:
                start_dt = datetime.fromisoformat(event["start_time"])
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
                else:
                    start_dt = start_dt.astimezone(timezone.utc)
            except (ValueError, TypeError) as e:
                log.warning(f"Reminder: could not parse start_time for event {event_id}: {event['start_time']!r} — {e}")
                continue

            offset_minutes = event.get("reminder_offset") or 15
            remind_at      = start_dt - timedelta(minutes=offset_minutes)

            # Quick skip: reminder time is more than 2 hours away — not our
            # concern yet. We check remind_at (not start_time) so that events
            # with large offsets (e.g. 1-day reminders) enter the loop at the
            # right moment, and events created close to their reminder window
            # are never skipped just because start_time > now+2h.
            if remind_at > now_plus_2h:
                log.debug(
                    f"Reminder: skipping event {event_id} '{event['title']}' — "
                    f"remind_at={remind_at.isoformat()} > lookahead"
                )
                continue

            diff = (now_utc - remind_at).total_seconds()

            log.info(
                f"Reminder check: event {event_id} '{event['title']}' | "
                f"start_utc={start_dt.isoformat()} | "
                f"remind_at={remind_at.isoformat()} | "
                f"diff={diff:.0f}s | "
                f"window=(-60, 300)"
            )

            # Window: fire if we're between 60s early and 300s late
            if not (-60 <= diff <= 300):
                log.info(f"Reminder: event {event_id} outside fire window (diff={diff:.0f}s) — skipping")
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

            # Advance recurring events and repost embed
            if event.get("is_recurring") and event.get("recur_rule"):
                next_start = compute_next_start(
                    event["start_time"], event["recur_rule"], event.get("recur_interval", 1)
                )
                if next_start:
                    with get_connection() as conn:
                        conn.execute(
                            "UPDATE events SET start_time=?, reminded_at=NULL WHERE id=?",
                            (next_start, event_id),
                        )
                        conn.commit()
                    log.info(f"Advanced recurring event {event_id} to next occurrence: {next_start}")
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

        # Use the guild's custom embed color for reminders
        from utils.database import get_guild_config
        from utils.embeds import get_guild_color
        cfg         = get_guild_config(channel.guild.id)
        guild_color = get_guild_color(cfg.get("embed_color") if cfg else None)

        embed = discord.Embed(
            title=f"⏰  Reminder: {event['title']} is starting soon!",
            description=(
                f"The event **{event['title']}** starts in "
                f"**{event.get('reminder_offset', 15)} minutes**.\n\n"
                "Check the original event post for details."
            ),
            color=guild_color,
        )

        # ── Role pings only — supports both new multi-role and legacy single-role ──
        ping_parts = []
        role_ids = _parse_role_ids(event)
        for role_id in role_ids:
            role = channel.guild.get_role(role_id)
            if role:
                ping_parts.append(role.mention)
            else:
                log.warning(f"Reminder: role {role_id} not found in guild {channel.guild.id}")

        # Note: individual RSVP pings deliberately removed — roles only at reminder time.
        # Per-user pings will be a future premium feature.

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