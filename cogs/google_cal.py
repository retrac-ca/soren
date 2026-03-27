"""
cogs/google_cal.py
===================
Google Calendar integration.

What this cog does
------------------
1. /gcal connect  — Walks the user through connecting a Google Calendar
                    (generates an OAuth URL they paste their code back from).
2. /gcal disconnect — Removes the stored token.
3. Background sync — Every 15 minutes, pulls new events from Google Calendar
                     and posts them in Discord (without RSVP buttons, as per spec).
4. push_to_gcal()  — Called by the events cog after a new event is created
                     via /newevent, to mirror it in Google Calendar.

NOTE
----
Google Calendar integration requires OAuth 2.0 credentials.
See README.md → Google Calendar Setup for the full steps.
The credentials file path is set in .env as GOOGLE_CREDENTIALS_FILE.
"""

import discord
from discord.ext import commands, tasks
from discord import SlashCommandGroup
import json
import os
import logging
from datetime import datetime, timezone

from utils.database import get_connection, upsert_guild_config, get_guild_config
from utils.embeds import build_success_embed, build_error_embed, COLOR_EVENT

log = logging.getLogger("soren.gcal")

# ── Optional import — Google libraries may not be installed ──────────────────
try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build as gcal_build
    GCAL_AVAILABLE = True
except ImportError:
    GCAL_AVAILABLE = False
    log.warning("google-auth / google-api-python-client not installed. "
                "Google Calendar integration will be unavailable.")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CREDS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")

# Store in-progress OAuth flows keyed by guild_id
_pending_flows: dict[int, object] = {}


def _get_service(token_json: str):
    """Build and return an authenticated Google Calendar service object."""
    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    return gcal_build("calendar", "v3", credentials=creds)


class GoogleCal(commands.Cog):
    """Google Calendar sync commands and background tasks."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        if GCAL_AVAILABLE:
            self.sync_loop.start()

    def cog_unload(self):
        if GCAL_AVAILABLE:
            self.sync_loop.cancel()

    gcal = SlashCommandGroup("gcal", "Google Calendar integration commands.")

    # ── /gcal connect ──────────────────────────────────────────────────────
    @gcal.command(name="connect", description="Connect a Google Calendar to this server.")
    @discord.default_permissions(administrator=True)
    async def gcal_connect(self, ctx: discord.ApplicationContext):
        """Step 1 of OAuth: send the user an authorization URL."""
        if not GCAL_AVAILABLE:
            await ctx.respond(
                embed=build_error_embed(
                    "Google Calendar libraries are not installed on this bot. "
                    "See README.md → Google Calendar Setup."
                ),
                ephemeral=True,
            )
            return

        if not os.path.exists(CREDS_FILE):
            await ctx.respond(
                embed=build_error_embed(
                    f"Missing `{CREDS_FILE}`. "
                    "Download your OAuth credentials from Google Cloud Console "
                    "and place the file in the bot's root directory."
                ),
                ephemeral=True,
            )
            return

        # Build the OAuth flow
        flow = Flow.from_client_secrets_file(
            CREDS_FILE, scopes=SCOPES,
            redirect_uri="http://localhost"
        )
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
        _pending_flows[ctx.guild.id] = flow

        embed = discord.Embed(
            title="🔗 Connect Google Calendar",
            description=(
                "1. Click the link below and sign in with Google.\n"
                "2. After authorizing, your browser will redirect to a `localhost` page "
                "that shows **'This site can't be reached'** — that's normal!\n"
                "3. Copy the `code=` value from the URL in your browser's address bar.\n"
                "   *(It looks like: `http://localhost/?code=4/0Ab...&scope=...`)*\n"
                "4. Run `/gcal verify` and paste just the code value.\n\n"
                f"[Click here to authorize Google Calendar]({auth_url})"
            ),
            color=COLOR_EVENT,
        )
        await ctx.respond(embed=embed, ephemeral=True)

    # ── /gcal verify ──────────────────────────────────────────────────────
    @gcal.command(name="verify", description="Complete Google Calendar connection with your auth code.")
    @discord.default_permissions(administrator=True)
    async def gcal_verify(
        self,
        ctx: discord.ApplicationContext,
        code: discord.Option(str, "The authorization code from Google."),
    ):
        """Step 2 of OAuth: exchange the auth code for a token."""
        flow = _pending_flows.pop(ctx.guild.id, None)
        if not flow:
            await ctx.respond(
                embed=build_error_embed("No pending connection. Run `/gcal connect` first."),
                ephemeral=True,
            )
            return

        try:
            flow.fetch_token(code=code.strip())
            token_json = flow.credentials.to_json()
        except Exception as e:
            await ctx.respond(
                embed=build_error_embed(f"Failed to exchange code: {e}"),
                ephemeral=True,
            )
            return

        # Get the user's primary calendar ID
        try:
            service  = _get_service(token_json)
            calendar = service.calendars().get(calendarId="primary").execute()
            cal_id   = calendar["id"]
        except Exception as e:
            await ctx.respond(
                embed=build_error_embed(f"Connected but couldn't fetch calendar: {e}"),
                ephemeral=True,
            )
            return

        upsert_guild_config(ctx.guild.id, gcal_token=token_json, gcal_id=cal_id)
        await ctx.respond(
            embed=build_success_embed(
                f"Google Calendar connected! Syncing with: `{cal_id}`"
            ),
            ephemeral=True,
        )

    # ── /gcal disconnect ──────────────────────────────────────────────────
    @gcal.command(name="disconnect", description="Remove Google Calendar connection.")
    @discord.default_permissions(administrator=True)
    async def gcal_disconnect(self, ctx: discord.ApplicationContext):
        upsert_guild_config(ctx.guild.id, gcal_token=None, gcal_id=None)
        await ctx.respond(
            embed=build_success_embed("Google Calendar disconnected."),
            ephemeral=True,
        )

    # ── Background sync loop ──────────────────────────────────────────────
    @tasks.loop(minutes=15)
    async def sync_loop(self):
        """
        Every 15 minutes: pull new Google Calendar events for all connected guilds
        and post them in the guild's system/first text channel.

        Events pulled from Google Calendar do NOT get RSVP buttons
        (as per spec — users must create events via /newevent for signups).
        """
        log.debug("Running Google Calendar sync...")

        with get_connection() as conn:
            guilds = conn.execute(
                "SELECT guild_id, gcal_token, gcal_id FROM guild_config "
                "WHERE gcal_token IS NOT NULL AND gcal_id IS NOT NULL"
            ).fetchall()

        for row in guilds:
            guild_id   = row["guild_id"]
            token_json = row["gcal_token"]
            cal_id     = row["gcal_id"]

            try:
                service = _get_service(token_json)
                now     = datetime.now(timezone.utc).isoformat()

                # Fetch events starting from now
                result = service.events().list(
                    calendarId=cal_id,
                    timeMin=now,
                    maxResults=10,
                    singleEvents=True,
                    orderBy="startTime",
                ).execute()

                for item in result.get("items", []):
                    gcal_event_id = item["id"]

                    # Check if we've already posted this event
                    with get_connection() as conn:
                        existing = conn.execute(
                            "SELECT id FROM events WHERE gcal_event_id=? AND guild_id=?",
                            (gcal_event_id, guild_id),
                        ).fetchone()

                    if existing:
                        continue  # Already synced

                    await self._post_gcal_event(guild_id, item, gcal_event_id)

            except Exception as e:
                log.error(f"Google Calendar sync failed for guild {guild_id}: {e}")

    @sync_loop.before_loop
    async def before_sync_loop(self):
        await self.bot.wait_until_ready()

    async def _post_gcal_event(self, guild_id: int, item: dict, gcal_event_id: str):
        """Post a Google Calendar event to the Discord server (no RSVP buttons)."""
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        # Find a channel to post in (system channel or first text channel)
        channel = guild.system_channel
        if not channel:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    channel = ch
                    break
        if not channel:
            return

        title       = item.get("summary", "Untitled Event")
        description = item.get("description", "")
        start       = item.get("start", {})
        start_str   = start.get("dateTime") or start.get("date", "")
        gcal_link   = item.get("htmlLink", "")

        embed = discord.Embed(
            title=f"📆  {title}",
            description=(
                f"*Imported from Google Calendar*\n\n"
                f"{description}\n\n"
                f"🕐 **Starts:** {start_str}\n"
                f"[View on Google Calendar]({gcal_link})"
                if gcal_link else f"🕐 **Starts:** {start_str}"
            ),
            color=COLOR_EVENT,
        )
        embed.set_footer(text="This event was imported from Google Calendar. "
                              "Use /newevent to create an event with RSVP signups.")

        msg = await channel.send(embed=embed)

        # Save to DB (no RSVP buttons / signups for gcal-sourced events)
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO events
                    (guild_id, channel_id, message_id, creator_id, title,
                     description, start_time, gcal_event_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id, channel.id, msg.id,
                    self.bot.user.id,    # Creator = the bot itself for gcal events
                    title, description, start_str, gcal_event_id,
                ),
            )
            conn.commit()

    # ── push_to_gcal (called externally by events cog) ────────────────────
    async def push_event_to_gcal(self, guild_id: int, event: dict):
        """
        Push a newly created Discord event to the connected Google Calendar.
        Called after /newevent successfully creates an event.
        """
        if not GCAL_AVAILABLE:
            return

        cfg = get_guild_config(guild_id)
        if not cfg or not cfg.get("gcal_token") or not cfg.get("gcal_id"):
            return  # No calendar connected

        try:
            service = _get_service(cfg["gcal_token"])
            body = {
                "summary": event["title"],
                "description": event.get("description", ""),
                "start": {"dateTime": event["start_time"], "timeZone": event.get("timezone", "UTC")},
                "end":   {"dateTime": event.get("end_time") or event["start_time"],
                          "timeZone": event.get("timezone", "UTC")},
            }
            created = service.events().insert(calendarId=cfg["gcal_id"], body=body).execute()
            # Save the Google Calendar event ID back to the database
            with get_connection() as conn:
                conn.execute(
                    "UPDATE events SET gcal_event_id=? WHERE id=?",
                    (created["id"], event["id"]),
                )
                conn.commit()
            log.info(f"Event {event['id']} pushed to Google Calendar as {created['id']}")
        except Exception as e:
            log.error(f"Failed to push event {event['id']} to Google Calendar: {e}")


def setup(bot: discord.Bot):
    bot.add_cog(GoogleCal(bot))
