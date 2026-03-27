"""
cogs/gcal_integrations.py
==========================
G-Cal Integrations — connect multiple Google Calendars to Soren,
each posting a weekly (or custom schedule) summary into a specified
Discord channel.

This feature is completely separate from the slash-command event system.
It does NOT create RSVP embeds. It only posts read-only digest summaries
of what's coming up in each connected Google Calendar.

How it works
------------
1. Admin runs /gcalint add  — starts OAuth flow for one calendar
2. Admin runs /gcalint verify — completes OAuth with the auth code
3. Soren stores the calendar token, label, channel, and schedule in
   the gcal_integrations table
4. A background loop checks every 15 minutes whether any integration
   is due for a post, and sends the weekly summary embed if so

Slash commands (admin only)
----------------------------
/gcalint add     — Start connecting a new Google Calendar
/gcalint verify  — Complete the OAuth flow with your auth code
/gcalint list    — Show all connected calendars for this server
/gcalint remove  — Disconnect a calendar by its ID
/gcalint pause   — Pause/resume a calendar's auto-posting
/gcalint post    — Manually trigger a summary post right now
"""

import discord
from discord.ext import commands, tasks
from discord import SlashCommandGroup
import json
import os
import logging
from datetime import datetime, timedelta, timezone

from utils.database import get_connection, get_guild_config
from utils.permissions import is_event_creator

log = logging.getLogger("soren.gcalint")

# ── Google library imports (optional — graceful fallback if not installed) ────
try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build as gcal_build
    GCAL_AVAILABLE = True
except ImportError:
    GCAL_AVAILABLE = False
    log.warning("Google libraries not installed. G-Cal Integrations unavailable.")

SCOPES    = ["https://www.googleapis.com/auth/calendar.readonly"]
CREDS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")

# Stores in-progress OAuth flows keyed by (guild_id, user_id)
# so multiple admins can be setting up calendars simultaneously
_pending_flows: dict[tuple, dict] = {}

# ── Schedule options shown in /gcalint add ────────────────────────────────────
SCHEDULE_OPTIONS = [
    discord.SelectOption(label="Weekly",  value="weekly",  emoji="📅",
                         description="Post once a week (Monday by default)"),
    discord.SelectOption(label="Daily",   value="daily",   emoji="🔁",
                         description="Post every morning"),
    discord.SelectOption(label="Custom",  value="custom",  emoji="⚙️",
                         description="Set a custom number of days between posts"),
]

# Days of week for weekly scheduling
DAY_OPTIONS = [
    discord.SelectOption(label="Monday",    value="monday"),
    discord.SelectOption(label="Tuesday",   value="tuesday"),
    discord.SelectOption(label="Wednesday", value="wednesday"),
    discord.SelectOption(label="Thursday",  value="thursday"),
    discord.SelectOption(label="Friday",    value="friday"),
    discord.SelectOption(label="Saturday",  value="saturday"),
    discord.SelectOption(label="Sunday",    value="sunday"),
]

# Maps weekday name → Python weekday int (Monday=0)
WEEKDAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


# ── Google Calendar helpers ───────────────────────────────────────────────────

def _get_service(token_json: str):
    """Build an authenticated Google Calendar service from a stored token JSON."""
    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    return gcal_build("calendar", "v3", credentials=creds)


def _fetch_week_events(token_json: str, calendar_id: str) -> list[dict]:
    """
    Fetch all events from the given Google Calendar for the next 7 days.
    Returns a list of dicts with 'title', 'start', 'end', 'description', 'location'.
    """
    service  = _get_service(token_json)
    now      = datetime.now(timezone.utc)
    week_end = now + timedelta(days=7)

    result = service.events().list(
        calendarId=calendar_id,
        timeMin=now.isoformat(),
        timeMax=week_end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=50,
    ).execute()

    events = []
    for item in result.get("items", []):
        start_raw = item.get("start", {})
        end_raw   = item.get("end",   {})

        # Events can be all-day (date) or timed (dateTime)
        start_str = start_raw.get("dateTime") or start_raw.get("date", "")
        end_str   = end_raw.get("dateTime")   or end_raw.get("date", "")

        # Format start time for display
        try:
            if "T" in start_str:
                dt = datetime.fromisoformat(start_str)
                display_time = dt.strftime("%a %b %d  •  %I:%M %p %Z").strip()
            else:
                dt = datetime.strptime(start_str, "%Y-%m-%d")
                display_time = dt.strftime("%a %b %d  •  All Day")
        except Exception:
            display_time = start_str

        events.append({
            "title":       item.get("summary", "Untitled Event"),
            "start":       display_time,
            "start_raw":   start_str,
            "description": item.get("description", ""),
            "location":    item.get("location", ""),
        })

    return events


def _build_summary_embed(integration: dict, events: list[dict]) -> discord.Embed:
    """
    Build the weekly summary embed for one calendar integration.
    Shows all events for the next 7 days, or a 'no events' message.
    """
    label    = integration["label"]
    now      = datetime.now(timezone.utc)
    week_end = now + timedelta(days=7)

    date_range = (
        f"{now.strftime('%b %d')} – {week_end.strftime('%b %d, %Y')}"
    )

    embed = discord.Embed(
        title=f"📆  {label} — Weekly Summary",
        description=f"**Upcoming events for {date_range}**",
        color=discord.Color.from_rgb(66, 133, 244),  # Google blue
    )

    if not events:
        embed.add_field(
            name="No events this week",
            value="Nothing scheduled in this calendar for the next 7 days.",
            inline=False,
        )
    else:
        for event in events:
            # Build the field value — include description/location if present
            value_parts = [f"🕐 {event['start']}"]
            if event.get("location"):
                value_parts.append(f"📍 {event['location']}")
            if event.get("description"):
                # Truncate long descriptions
                desc = event["description"][:120]
                if len(event["description"]) > 120:
                    desc += "…"
                value_parts.append(desc)

            embed.add_field(
                name=event["title"],
                value="\n".join(value_parts),
                inline=False,
            )

    embed.set_footer(
        text=f"G-Cal Integration  •  Schedule: {integration['schedule'].capitalize()}  "
             f"•  {len(events)} event(s) this week"
    )
    return embed


def _is_due(integration: dict) -> bool:
    """
    Return True if this integration is due for a post right now.
    Checks the schedule type and compares against last_posted.
    """
    if not integration.get("active", 1):
        return False   # Paused

    now        = datetime.now(timezone.utc)
    post_hour  = integration.get("post_hour", 9)
    schedule   = integration.get("schedule", "weekly")
    last_posted = integration.get("last_posted")

    # Only post during the configured hour (±30 min window)
    if abs(now.hour - post_hour) > 0:
        return False

    # If never posted, it's due
    if not last_posted:
        return True

    try:
        last_dt = datetime.fromisoformat(last_posted).replace(tzinfo=timezone.utc)
    except ValueError:
        return True   # Corrupt timestamp — post now

    elapsed_days = (now - last_dt).days

    if schedule == "daily":
        return elapsed_days >= 1

    elif schedule == "weekly":
        # Only post on the configured day of week
        target_weekday = WEEKDAY_MAP.get(
            integration.get("post_day", "monday"), 0
        )
        return now.weekday() == target_weekday and elapsed_days >= 6

    elif schedule == "custom":
        interval = integration.get("custom_interval", 7)
        return elapsed_days >= interval

    return False


# ── Setup modals ──────────────────────────────────────────────────────────────

class IntegrationDetailModal(discord.ui.Modal):
    """
    Second step of /gcalint add.
    Collects the calendar label, target channel ID, post hour,
    and (if custom schedule) the interval in days.
    """

    def __init__(self, schedule: str, post_day: str, guild_id: int,
                 user_id: int, *args, **kwargs):
        super().__init__(title="G-Cal Integration Setup", *args, **kwargs)
        self.schedule  = schedule
        self.post_day  = post_day
        self.guild_id  = guild_id
        self.user_id   = user_id

        self.add_item(discord.ui.InputText(
            label="Calendar Label",
            placeholder="e.g. MMA Events, Hockey Schedule, Soccer",
            max_length=60,
        ))
        self.add_item(discord.ui.InputText(
            label="Discord Channel ID",
            placeholder="Right-click a channel → Copy Channel ID",
            max_length=20,
        ))
        self.add_item(discord.ui.InputText(
            label="Post Hour (0–23, UTC)",
            placeholder="e.g. 9 for 9:00 AM UTC",
            max_length=2,
        ))

        # Only show interval field for custom schedule
        if schedule == "custom":
            self.add_item(discord.ui.InputText(
                label="Interval (days between posts)",
                placeholder="e.g. 3",
                max_length=3,
            ))

    async def callback(self, interaction: discord.Interaction):
        label      = self.children[0].value.strip()
        channel_id_raw = self.children[1].value.strip()
        post_hour_raw  = self.children[2].value.strip()

        # Validate channel ID
        try:
            channel_id = int(channel_id_raw)
        except ValueError:
            await interaction.response.send_message(
                "❌ Invalid channel ID. Right-click a channel and choose **Copy Channel ID**.",
                ephemeral=True,
            )
            return

        # Validate post hour
        try:
            post_hour = int(post_hour_raw)
            if not 0 <= post_hour <= 23:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ Post hour must be a number between 0 and 23.",
                ephemeral=True,
            )
            return

        # Custom interval
        custom_interval = 7
        if self.schedule == "custom" and len(self.children) > 3:
            try:
                custom_interval = max(1, int(self.children[3].value.strip()))
            except ValueError:
                custom_interval = 7

        # Store the setup details in pending_flows so /gcalint verify can use them
        key = (self.guild_id, self.user_id)
        if key not in _pending_flows:
            _pending_flows[key] = {}

        _pending_flows[key].update({
            "label":           label,
            "channel_id":      channel_id,
            "post_hour":       post_hour,
            "schedule":        self.schedule,
            "post_day":        self.post_day,
            "custom_interval": custom_interval,
        })

        # Now start the OAuth flow and send the auth URL
        if not GCAL_AVAILABLE:
            await interaction.response.send_message(
                "❌ Google libraries are not installed on this bot. "
                "See README.md → Google Calendar Setup.",
                ephemeral=True,
            )
            return

        if not os.path.exists(CREDS_FILE):
            await interaction.response.send_message(
                f"❌ Missing `{CREDS_FILE}`. Download your OAuth credentials "
                "from Google Cloud Console and place the file in the bot's root folder.",
                ephemeral=True,
            )
            return

        flow = Flow.from_client_secrets_file(
            CREDS_FILE,
            scopes=SCOPES,
            redirect_uri="http://localhost",
        )
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
        _pending_flows[key]["flow"] = flow

        embed = discord.Embed(
            title="🔗  Step 3 of 3 — Authorize Google Calendar",
            description=(
                f"**Calendar:** {label}\n"
                f"**Channel:** <#{channel_id}>\n"
                f"**Schedule:** {self.schedule.capitalize()}\n\n"
                "**To complete setup:**\n"
                "1. Click the link below and sign in with Google\n"
                "2. Choose the Google account that owns the calendar\n"
                "3. After authorizing, your browser will redirect to a `localhost` page "
                "that shows **'This site can't be reached'** — that's normal!\n"
                "4. Copy the `code=` value from the URL in your browser's address bar\n"
                "   *(looks like: `http://localhost/?code=4/0Ab...&scope=...`)*\n"
                "5. Run `/gcalint verify` and paste just the code value\n\n"
                f"[🔑 Click here to authorize]({auth_url})"
            ),
            color=discord.Color.from_rgb(66, 133, 244),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Schedule selector view (Step 1) ──────────────────────────────────────────

class ScheduleSelectView(discord.ui.View):
    """First step: choose posting schedule."""

    def __init__(self, guild_id: int, author_id: int):
        super().__init__(timeout=120)
        self.guild_id  = guild_id
        self.author_id = author_id
        self.schedule  = None
        self.post_day  = "monday"

    @discord.ui.select(placeholder="Choose posting schedule...", options=SCHEDULE_OPTIONS)
    async def schedule_select(self, select: discord.ui.Select,
                               interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran this command can use this menu.",
                ephemeral=True,
            )
            return

        self.schedule = select.values[0]

        # For weekly, ask which day to post on
        if self.schedule == "weekly":
            day_view = DaySelectView(
                guild_id=self.guild_id,
                author_id=self.author_id,
                schedule=self.schedule,
            )
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="📅  Step 2 of 3 — Pick a day",
                    description="Which day of the week should the summary be posted?",
                    color=discord.Color.from_rgb(66, 133, 244),
                ),
                view=day_view,
            )
        else:
            # Daily or custom — skip day selection, go straight to detail modal
            self.stop()
            await interaction.response.send_modal(
                IntegrationDetailModal(
                    schedule=self.schedule,
                    post_day="monday",
                    guild_id=self.guild_id,
                    user_id=interaction.user.id,
                )
            )


class DaySelectView(discord.ui.View):
    """Second step (weekly only): choose the day of week."""

    def __init__(self, guild_id: int, author_id: int, schedule: str):
        super().__init__(timeout=120)
        self.guild_id  = guild_id
        self.author_id = author_id
        self.schedule  = schedule

    @discord.ui.select(placeholder="Choose day of week...", options=DAY_OPTIONS)
    async def day_select(self, select: discord.ui.Select,
                          interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran this command can use this menu.",
                ephemeral=True,
            )
            return

        self.stop()
        await interaction.response.send_modal(
            IntegrationDetailModal(
                schedule=self.schedule,
                post_day=select.values[0],
                guild_id=self.guild_id,
                user_id=interaction.user.id,
            )
        )


# ── Calendar picker (shown after OAuth verify) ───────────────────────────────

class CalendarPickerView(discord.ui.View):
    """
    Shown after a successful OAuth exchange.
    Lists all Google Calendars on the account and lets the admin
    pick exactly which one to connect to this integration.
    """

    def __init__(self, author_id: int, calendars: list[dict],
                 token_json: str, pending: dict, guild_id: int):
        super().__init__(timeout=120)
        self.author_id  = author_id
        self.token_json = token_json
        self.pending    = pending
        self.guild_id   = guild_id

        # Build select options — cap at 25 (Discord limit)
        options = []
        for cal in calendars[:25]:
            name    = cal.get("summary", "Unnamed Calendar")
            cal_id  = cal["id"]
            primary = cal.get("primary", False)
            options.append(discord.SelectOption(
                label=name[:100],
                value=cal_id,
                description="Primary calendar" if primary else cal_id[:100],
                emoji="⭐" if primary else "📅",
            ))

        self.add_item(self._make_select(options))

    def _make_select(self, options):
        select = discord.ui.Select(
            placeholder="Choose which calendar to connect...",
            options=options,
        )
        select.callback = self._on_select
        return select

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran this command can use this menu.",
                ephemeral=True,
            )
            return

        calendar_id = interaction.data["values"][0]
        # Find the display name of the chosen calendar
        calendar_name = calendar_id
        for opt in self.children[0].options:
            if opt.value == calendar_id:
                calendar_name = opt.label
                break

        self.stop()

        # Ensure guild_config row exists (FK requirement)
        with get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)",
                (self.guild_id,),
            )
            conn.commit()

        # Save the integration
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO gcal_integrations
                    (guild_id, label, calendar_id, gcal_token, channel_id,
                     schedule, custom_interval, post_day, post_hour, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    self.guild_id,
                    self.pending["label"],
                    calendar_id,
                    self.token_json,
                    self.pending["channel_id"],
                    self.pending["schedule"],
                    self.pending["custom_interval"],
                    self.pending["post_day"],
                    self.pending["post_hour"],
                ),
            )
            conn.commit()

        embed = discord.Embed(
            title="✅  G-Cal Integration Connected!",
            description=(
                f"**Label:** {self.pending['label']}\n"
                f"**Calendar:** {calendar_name}\n"
                f"**Posting to:** <#{self.pending['channel_id']}>\n"
                f"**Schedule:** {self.pending['schedule'].capitalize()}"
                + (f" ({self.pending['post_day'].capitalize()}s)"
                   if self.pending["schedule"] == "weekly" else "")
                + f"\n**Post Hour:** {self.pending['post_hour']}:00 UTC\n\n"
                "Soren will now automatically post summaries of this "
                "calendar's events into the specified channel.\n\n"
                "Use `/gcalint post` to trigger a manual summary right now."
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=None)


# ── Cog ───────────────────────────────────────────────────────────────────────

class GCalIntegrations(commands.Cog):
    """
    G-Cal Integrations — multi-calendar read-only weekly summary system.
    Completely separate from the slash-command event / RSVP system.
    """

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.summary_loop.start()

    def cog_unload(self):
        self.summary_loop.cancel()

    # ── Slash command group ───────────────────────────────────────────────
    gcalint = SlashCommandGroup(
        "gcalint",
        "G-Cal Integration commands — connect Google Calendars for auto-summaries.",
    )

    # ── /gcalint add ──────────────────────────────────────────────────────
    @gcalint.command(name="add", description="Connect a new Google Calendar for auto-summaries.")
    @discord.default_permissions(administrator=True)
    async def gcalint_add(self, ctx: discord.ApplicationContext):
        """
        Step 1 of 3: Show the schedule type selector.
        Step 2 of 3: (weekly only) Pick a day of week.
        Step 3 of 3: Fill in label, channel, hour → get OAuth URL.
        Then run /gcalint verify to complete the connection.
        """
        if not GCAL_AVAILABLE:
            await ctx.respond(
                "❌ Google libraries are not installed. See README.md → Google Calendar Setup.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📅  G-Cal Integration — Step 1 of 3",
            description=(
                "Connect a Google Calendar to post automatic summaries into a Discord channel.\n\n"
                "**Select the posting schedule below.**"
            ),
            color=discord.Color.from_rgb(66, 133, 244),
        )
        view = ScheduleSelectView(guild_id=ctx.guild.id, author_id=ctx.author.id)
        await ctx.respond(embed=embed, view=view, ephemeral=True)

    # ── /gcalint verify ───────────────────────────────────────────────────
    @gcalint.command(
        name="verify",
        description="Complete a Google Calendar connection with your auth code."
    )
    @discord.default_permissions(administrator=True)
    async def gcalint_verify(
        self,
        ctx: discord.ApplicationContext,
        code: discord.Option(str, "The authorization code from Google.", required=True),
    ):
        """Exchanges the OAuth code for a token and saves the integration."""
        key     = (ctx.guild.id, ctx.author.id)
        pending = _pending_flows.get(key)

        if not pending or "flow" not in pending:
            await ctx.respond(
                "❌ No pending setup found. Run `/gcalint add` first.",
                ephemeral=True,
            )
            return

        flow = pending["flow"]

        # Exchange the auth code for a token
        try:
            flow.fetch_token(code=code.strip())
            token_json = flow.credentials.to_json()
        except Exception as e:
            await ctx.respond(
                f"❌ Failed to exchange authorization code: `{e}`\n"
                "Make sure you copied the full code and try `/gcalint add` again.",
                ephemeral=True,
            )
            return

        # Verify we can reach the calendar and get its name
        try:
            service  = _get_service(token_json)
            cal_list = service.calendarList().list().execute()
            calendars = cal_list.get("items", [])
        except Exception as e:
            await ctx.respond(
                f"❌ Connected but couldn't fetch calendar list: `{e}`",
                ephemeral=True,
            )
            return

        # Show the calendar picker — let the admin choose which calendar to connect
        view = CalendarPickerView(
            author_id=ctx.author.id,
            calendars=calendars,
            token_json=token_json,
            pending=pending,
            guild_id=ctx.guild.id,
        )

        # Clean up the pending flow now that we have the token
        del _pending_flows[key]

        embed = discord.Embed(
            title="📅  Choose a Calendar",
            description=(
                f"Google account connected! Found **{len(calendars)}** calendar(s).\n\n"
                "Select which calendar you want Soren to use for this integration:"
            ),
            color=discord.Color.from_rgb(66, 133, 244),
        )
        await ctx.respond(embed=embed, view=view, ephemeral=True)

    # ── /gcalint list ─────────────────────────────────────────────────────
    @gcalint.command(name="list", description="Show all connected Google Calendars for this server.")
    @discord.default_permissions(administrator=True)
    async def gcalint_list(self, ctx: discord.ApplicationContext):
        """Lists all G-Cal integrations configured for this server."""
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM gcal_integrations WHERE guild_id=? ORDER BY id ASC",
                (ctx.guild.id,),
            ).fetchall()

        if not rows:
            await ctx.respond(
                "No G-Cal integrations set up yet. Use `/gcalint add` to connect one.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📆  Connected Google Calendars",
            color=discord.Color.from_rgb(66, 133, 244),
        )

        for row in rows:
            status = "✅ Active" if row["active"] else "⏸️ Paused"
            schedule_str = row["schedule"].capitalize()
            if row["schedule"] == "weekly":
                schedule_str += f" ({row['post_day'].capitalize()}s @ {row['post_hour']}:00 UTC)"
            elif row["schedule"] == "custom":
                schedule_str += f" (every {row['custom_interval']} days @ {row['post_hour']}:00 UTC)"
            else:
                schedule_str += f" (@ {row['post_hour']}:00 UTC)"

            last = row["last_posted"] or "Never"
            embed.add_field(
                name=f"[ID: {row['id']}]  {row['label']}  —  {status}",
                value=(
                    f"📢 <#{row['channel_id']}>\n"
                    f"🗓️ {schedule_str}\n"
                    f"🕐 Last posted: {last}"
                ),
                inline=False,
            )

        embed.set_footer(
            text="Use /gcalint remove <id> to disconnect  •  /gcalint pause <id> to pause/resume"
        )
        await ctx.respond(embed=embed, ephemeral=True)

    # ── /gcalint remove ───────────────────────────────────────────────────
    @gcalint.command(name="remove", description="Disconnect a Google Calendar integration.")
    @discord.default_permissions(administrator=True)
    async def gcalint_remove(
        self,
        ctx: discord.ApplicationContext,
        integration_id: discord.Option(int, "The integration ID (from /gcalint list).", required=True),
    ):
        """Permanently removes a G-Cal integration."""
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM gcal_integrations WHERE id=? AND guild_id=?",
                (integration_id, ctx.guild.id),
            ).fetchone()

        if not row:
            await ctx.respond(
                f"❌ No integration found with ID `{integration_id}` in this server.",
                ephemeral=True,
            )
            return

        with get_connection() as conn:
            conn.execute(
                "DELETE FROM gcal_integrations WHERE id=?", (integration_id,)
            )
            conn.commit()

        await ctx.respond(
            f"✅ **{row['label']}** has been disconnected and removed.",
            ephemeral=True,
        )

    # ── /gcalint pause ────────────────────────────────────────────────────
    @gcalint.command(name="pause", description="Pause or resume a calendar's auto-posting.")
    @discord.default_permissions(administrator=True)
    async def gcalint_pause(
        self,
        ctx: discord.ApplicationContext,
        integration_id: discord.Option(int, "The integration ID (from /gcalint list).", required=True),
    ):
        """Toggles the active state of a G-Cal integration."""
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM gcal_integrations WHERE id=? AND guild_id=?",
                (integration_id, ctx.guild.id),
            ).fetchone()

        if not row:
            await ctx.respond(
                f"❌ No integration found with ID `{integration_id}`.",
                ephemeral=True,
            )
            return

        new_state = 0 if row["active"] else 1
        with get_connection() as conn:
            conn.execute(
                "UPDATE gcal_integrations SET active=? WHERE id=?",
                (new_state, integration_id),
            )
            conn.commit()

        state_str = "▶️ resumed" if new_state else "⏸️ paused"
        await ctx.respond(
            f"**{row['label']}** has been **{state_str}**.",
            ephemeral=True,
        )

    # ── /gcalint post ─────────────────────────────────────────────────────
    @gcalint.command(name="post", description="Manually post a calendar summary right now.")
    @discord.default_permissions(administrator=True)
    async def gcalint_post(
        self,
        ctx: discord.ApplicationContext,
        integration_id: discord.Option(int, "The integration ID (from /gcalint list).", required=True),
    ):
        """Immediately triggers a summary post for the specified integration."""
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM gcal_integrations WHERE id=? AND guild_id=?",
                (integration_id, ctx.guild.id),
            ).fetchone()

        if not row:
            await ctx.respond(
                f"❌ No integration found with ID `{integration_id}`.",
                ephemeral=True,
            )
            return

        await ctx.defer(ephemeral=True)   # May take a moment to fetch from Google
        await self._post_summary(dict(row))
        await ctx.followup.send(
            f"✅ Summary for **{row['label']}** posted in <#{row['channel_id']}>.",
            ephemeral=True,
        )

    # ── Background loop ───────────────────────────────────────────────────
    @tasks.loop(minutes=15)
    async def summary_loop(self):
        """
        Runs every 15 minutes.
        Checks every active integration to see if it's due for a post,
        and sends the summary embed if so.
        """
        if not GCAL_AVAILABLE:
            return

        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM gcal_integrations WHERE active=1"
            ).fetchall()

        for row in rows:
            integration = dict(row)
            if _is_due(integration):
                log.info(
                    f"Posting summary for integration {integration['id']}: {integration['label']}"
                )
                await self._post_summary(integration)

    @summary_loop.before_loop
    async def before_summary_loop(self):
        await self.bot.wait_until_ready()

    # ── Core post logic ───────────────────────────────────────────────────
    async def _post_summary(self, integration: dict):
        """
        Fetch events from Google Calendar and post the summary embed
        into the configured Discord channel.
        """
        guild   = self.bot.get_guild(integration["guild_id"])
        channel = self.bot.get_channel(integration["channel_id"])

        if not guild or not channel:
            log.warning(
                f"Integration {integration['id']}: guild or channel not found, skipping."
            )
            return

        try:
            events = _fetch_week_events(
                integration["gcal_token"],
                integration["calendar_id"],
            )
        except Exception as e:
            log.error(
                f"Failed to fetch events for integration {integration['id']}: {e}"
            )
            # Post an error notice to the channel so admins know something's wrong
            await channel.send(
                embed=discord.Embed(
                    title=f"⚠️  {integration['label']} — Sync Error",
                    description=(
                        "Soren couldn't fetch events from this Google Calendar.\n"
                        f"Error: `{e}`\n\n"
                        "The OAuth token may have expired. "
                        "Run `/gcalint remove` then `/gcalint add` to reconnect."
                    ),
                    color=discord.Color.red(),
                )
            )
            return

        embed = _build_summary_embed(integration, events)
        await channel.send(embed=embed)

        # Update last_posted timestamp
        with get_connection() as conn:
            conn.execute(
                "UPDATE gcal_integrations SET last_posted=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), integration["id"]),
            )
            conn.commit()


def setup(bot: discord.Bot):
    bot.add_cog(GCalIntegrations(bot))
