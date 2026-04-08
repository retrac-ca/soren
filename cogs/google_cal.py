"""
cogs/google_cal.py
===================
Google Calendar integration.

What this cog does
------------------
1. /gcal connect    — OAuth flow: generates auth URL
2. /gcal verify     — Exchanges auth code, then shows a calendar picker dropdown
3. /gcal disconnect — Removes the stored token and calendar ID
4. Background sync  — Every 15 minutes, pulls new Google Calendar events and posts them
5. push_to_gcal()   — Called by the events cog after /newevent to mirror it in Google Calendar

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

SCOPES     = ["https://www.googleapis.com/auth/calendar"]
CREDS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")

# Store in-progress OAuth flows keyed by guild_id
_pending_flows: dict[int, object] = {}


def _get_service(token_json: str):
    """Build and return an authenticated Google Calendar service object."""
    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    return gcal_build("calendar", "v3", credentials=creds)


# ── Calendar picker view (shown after /gcal verify) ───────────────────────────

class GcalPickerView(discord.ui.View):
    """
    Dropdown listing all calendars on the authenticated Google account.
    User selects which one to use for two-way sync with /newevent.
    Capped at 25 (Discord select limit).
    """

    def __init__(self, guild_id: int, token_json: str, calendars: list[dict]):
        super().__init__(timeout=120)
        self.guild_id   = guild_id
        self.token_json = token_json

        options = []
        for cal in calendars[:25]:
            is_primary = cal.get("primary", False)
            emoji      = "⭐" if is_primary else "📅"
            options.append(discord.SelectOption(
                label=cal.get("summary", "Unnamed Calendar")[:100],
                value=cal["id"],
                emoji=emoji,
                description="Primary calendar" if is_primary else None,
            ))

        select = discord.ui.Select(
            placeholder="Choose a calendar to sync with /newevent...",
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        cal_id   = interaction.data["values"][0]
        cal_name = next(
            (o.label for o in self.children[0].options if o.value == cal_id),
            cal_id,
        )

        upsert_guild_config(self.guild_id, gcal_token=self.token_json, gcal_id=cal_id)
        log.info(f"gcal: guild {self.guild_id} connected primary calendar '{cal_name}' ({cal_id})")

        self.stop()
        await interaction.response.edit_message(
            embed=build_success_embed(
                f"Google Calendar connected!\n\n"
                f"**Syncing with:** `{cal_name}`\n\n"
                "New events created with `/newevent` will automatically be pushed to this calendar."
            ),
            view=None,
        )


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

        flow = Flow.from_client_secrets_file(
            CREDS_FILE, scopes=SCOPES, redirect_uri="http://localhost"
        )
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
        _pending_flows[ctx.guild.id] = flow

        embed = discord.Embed(
            title="🔗  Connect Google Calendar",
            description=(
                "**Steps:**\n"
                "1. Click the link below and sign in with Google.\n"
                "2. After authorizing, your browser will show **'This site can't be reached'** — that's normal!\n"
                "3. Copy the `code=` value from your browser's address bar.\n"
                "   *(The URL looks like: `http://localhost/?code=4/0Ab...&scope=...`)*\n"
                "4. Run `/gcal verify` and paste just the code.\n\n"
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
        """Step 2 of OAuth: exchange the auth code, then show a calendar picker."""
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
                embed=build_error_embed(f"Failed to exchange auth code: `{e}`"),
                ephemeral=True,
            )
            return

        # Fetch all calendars for the picker
        try:
            service   = _get_service(token_json)
            cal_list  = service.calendarList().list().execute()
            calendars = cal_list.get("items", [])
        except Exception as e:
            await ctx.respond(
                embed=build_error_embed(f"Authorized but couldn't fetch calendar list: `{e}`"),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📅  Choose a Calendar",
            description=(
                "Authorization successful! Select which calendar to use for two-way sync.\n"
                "Events created with `/newevent` will be pushed into this calendar.\n\n"
                "⭐ = Primary calendar"
            ),
            color=COLOR_EVENT,
        )
        await ctx.respond(
            embed=embed,
            view=GcalPickerView(
                guild_id=ctx.guild.id,
                token_json=token_json,
                calendars=calendars,
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
        and post them as informational embeds (no RSVP buttons).
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

                result = service.events().list(
                    calendarId=cal_id,
                    timeMin=now,
                    maxResults=10,
                    singleEvents=True,
                    orderBy="startTime",
                ).execute()

                for item in result.get("items", []):
                    gcal_event_id = item["id"]

                    with get_connection() as conn:
                        existing = conn.execute(
                            "SELECT id FROM events WHERE gcal_event_id=? AND guild_id=?",
                            (gcal_event_id, guild_id),
                        ).fetchone()

                    if existing:
                        continue

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
                    self.bot.user.id,
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
            return  # No calendar connected — silently skip

        try:
            service = _get_service(cfg["gcal_token"])
            body = {
                "summary":     event["title"],
                "description": event.get("description", ""),
                "start": {
                    "dateTime": event["start_time"],
                    "timeZone": event.get("timezone", "UTC"),
                },
                "end": {
                    "dateTime": event.get("end_time") or event["start_time"],
                    "timeZone": event.get("timezone", "UTC"),
                },
            }
            created = service.events().insert(calendarId=cfg["gcal_id"], body=body).execute()
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