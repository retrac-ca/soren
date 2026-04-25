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

from utils.database import get_connection, parse_role_ids
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

        # ── Archive threads for ended non-recurring events ────────────────
        # Recurring events are handled by the advancement pass below.
        with get_connection() as conn:
            threaded = conn.execute(
                "SELECT id, thread_id, end_time, start_time FROM events "
                "WHERE thread_id IS NOT NULL AND thread_archived = 0 AND is_recurring = 0"
            ).fetchall()

        for trow in threaded:
            tev = dict(trow)
            archive_after_str = tev.get("end_time") or tev.get("start_time")
            if not archive_after_str:
                continue
            try:
                archive_after = datetime.fromisoformat(archive_after_str)
                if archive_after.tzinfo is None:
                    archive_after = archive_after.replace(tzinfo=timezone.utc)
                else:
                    archive_after = archive_after.astimezone(timezone.utc)
            except (ValueError, TypeError):
                continue
            if now_utc < archive_after:
                continue
            # Mark first so a failed Discord call doesn't cause repeated attempts
            with get_connection() as conn:
                conn.execute("UPDATE events SET thread_archived=1 WHERE id=?", (tev["id"],))
                conn.commit()
            try:
                thread = self.bot.get_channel(tev["thread_id"]) or await self.bot.fetch_channel(tev["thread_id"])
                if thread:
                    await thread.edit(archived=True)
                    log.info(f"Archived thread {tev['thread_id']} for ended event {tev['id']}")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"reminder_loop: could not archive thread {tev['thread_id']} for event {tev['id']}: {e}")

        # ── Advance recurring events whose current occurrence has ended ──────
        with get_connection() as conn:
            recurring = conn.execute(
                "SELECT * FROM events WHERE is_recurring=1 AND reminded_at IS NOT NULL AND is_cancelled=0"
            ).fetchall()

        for row in recurring:
            event    = dict(row)
            event_id = event["id"]

            # Determine when this occurrence ends; fall back to start_time if no end_time
            end_str = event.get("end_time") or event.get("start_time")
            if not end_str:
                continue
            try:
                end_dt = datetime.fromisoformat(end_str)
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                else:
                    end_dt = end_dt.astimezone(timezone.utc)
            except (ValueError, TypeError):
                continue

            if now_utc < end_dt:
                continue

            # Occurrence has ended — compute the next one
            next_start = compute_next_start(
                event["start_time"], event["recur_rule"], event.get("recur_interval", 1)
            )
            if next_start:
                # Carry end_time and rsvp_cutoff forward relative to the new start
                new_end    = None
                new_cutoff = None
                try:
                    orig_start    = datetime.fromisoformat(event["start_time"])
                    next_start_dt = datetime.fromisoformat(next_start)
                    if orig_start.tzinfo is None:
                        orig_start = orig_start.replace(tzinfo=timezone.utc)
                    if next_start_dt.tzinfo is None:
                        next_start_dt = next_start_dt.replace(tzinfo=timezone.utc)

                    if event.get("end_time"):
                        orig_end = datetime.fromisoformat(event["end_time"])
                        if orig_end.tzinfo is None:
                            orig_end = orig_end.replace(tzinfo=timezone.utc)
                        new_end = (next_start_dt + (orig_end - orig_start)).isoformat()

                    if event.get("rsvp_cutoff"):
                        orig_cutoff = datetime.fromisoformat(event["rsvp_cutoff"])
                        if orig_cutoff.tzinfo is None:
                            orig_cutoff = orig_cutoff.replace(tzinfo=timezone.utc)
                        new_cutoff = (next_start_dt + (orig_cutoff - orig_start)).isoformat()

                except (ValueError, TypeError):
                    pass

                with get_connection() as conn:
                    conn.execute(
                        "UPDATE events SET start_time=?, end_time=?, rsvp_cutoff=?, reminded_at=NULL WHERE id=?",
                        (next_start, new_end, new_cutoff, event_id),
                    )
                    conn.commit()
                log.info(f"Advanced recurring event {event_id} to next occurrence: {next_start}")
                await repost_recurring_embed(self.bot, event_id)
            else:
                # Series is complete — archive the thread if one is still open
                log.info(f"No more occurrences for recurring event {event_id}")
                if event.get("thread_id") and not event.get("thread_archived"):
                    with get_connection() as conn:
                        conn.execute("UPDATE events SET thread_archived=1 WHERE id=?", (event_id,))
                        conn.commit()
                    try:
                        thread = self.bot.get_channel(event["thread_id"]) or await self.bot.fetch_channel(event["thread_id"])
                        if thread:
                            await thread.edit(archived=True)
                            log.info(f"Archived thread {event['thread_id']} for completed recurring event {event_id}")
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                        log.warning(f"reminder_loop: could not archive thread for event {event_id}: {e}")

        with get_connection() as conn:
            events = conn.execute(
                "SELECT * FROM events WHERE reminded_at IS NULL"
            ).fetchall()

        log.info(f"Reminder loop tick: {len(events)} unreminded event(s) found, now_utc={now_utc.isoformat()}")

        for row in events:
            event    = dict(row)
            event_id = event["id"]

            # Skip cancelled events — they should never send reminders
            if event.get("is_cancelled"):
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
                log.info(
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
        role_ids = parse_role_ids(event)
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