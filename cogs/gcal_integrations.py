## soren/cogs/gcal_integrations.py


"""
cogs/gcal_integrations.py
==========================
Google Calendar Integrations — multi-calendar weekly digest summaries.

Each integration connects one Google Calendar to a Discord channel and
auto-posts a summary of upcoming events on a configurable schedule.
This is completely separate from the slash-command event system (/newevent).
No RSVP buttons — purely informational digest posts.

Commands
--------
/gcalint add     — 3-step setup wizard (schedule → day → label/channel/hour → OAuth)
/gcalint verify  — Complete OAuth and pick which calendar to connect
/gcalint list    — Show all connected calendars and their status
/gcalint remove  — Disconnect a calendar integration
/gcalint pause   — Toggle a calendar on/off
/gcalint post    — Manually trigger a summary post immediately
"""

import discord
from discord.ext import commands, tasks
from discord import SlashCommandGroup
import json
import os
import re
import html as html_module
import logging
from datetime import datetime, timedelta, timezone
from math import ceil

from utils.database import get_connection, upsert_guild_config, is_premium

log = logging.getLogger("soren.gcalint")

# ── Optional Google imports ───────────────────────────────────────────────────
try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build as gcal_build
    GCAL_AVAILABLE = True
except ImportError:
    GCAL_AVAILABLE = False
    log.warning("google-auth / google-api-python-client not installed. "
                "G-Cal Integrations will be unavailable.")

SCOPES     = ["https://www.googleapis.com/auth/calendar.readonly"]
CREDS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")

EVENTS_PER_PAGE   = 8
FREE_GCAL_LIMIT   = 2
PREMIUM_GCAL_LIMIT = 10

# Pending OAuth flows keyed by guild_id
_pending_flows: dict[int, object] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_service(token_json: str):
    """Build an authenticated read-only Google Calendar service."""
    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    return gcal_build("calendar", "v3", credentials=creds)


def _clean_html(text: str) -> str:
    """
    Strip HTML from Google Calendar event descriptions.
    Converts <br>/<p> to newlines, extracts anchor text, strips remaining tags,
    decodes HTML entities, and collapses excessive blank lines.
    """
    if not text:
        return ""
    # Convert block-level tags to newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    # Extract anchor display text (drop the URL)
    text = re.sub(r'<a[^>]*>(.*?)</a>', r'\1', text, flags=re.IGNORECASE | re.DOTALL)
    # Strip all remaining HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode HTML entities
    text = html_module.unescape(text)
    # Collapse 3+ consecutive newlines down to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _format_event_time(item: dict) -> str:
    """Return a human-readable time string from a Google Calendar event item."""
    start = item.get("start", {})
    if "dateTime" in start:
        try:
            dt = datetime.fromisoformat(start["dateTime"])
            return dt.strftime("%a %b %d, %Y  •  %I:%M %p %Z").strip()
        except Exception:
            return start["dateTime"]
    elif "date" in start:
        try:
            dt = datetime.strptime(start["date"], "%Y-%m-%d")
            return dt.strftime("%a %b %d, %Y  •  All Day")
        except Exception:
            return start["date"]
    return "Unknown time"


async def _fetch_week_events(token_json: str, calendar_id: str) -> list[dict]:
    """Fetch all events from a Google Calendar for the next 7 days."""
    try:
        service  = _get_service(token_json)
        now      = datetime.now(timezone.utc)
        week_end = now + timedelta(days=7)

        result = service.events().list(
            calendarId=calendar_id,
            timeMin=now.isoformat(),
            timeMax=week_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=100,
        ).execute()

        events = []
        for item in result.get("items", []):
            events.append({
                "title":    item.get("summary", "Untitled"),
                "time":     _format_event_time(item),
                "location": _clean_html(item.get("location", "")),
            })
        return events
    except Exception as e:
        log.error(f"Failed to fetch events for calendar {calendar_id}: {e}")
        return []


def _build_summary_embed(
    label: str,
    events: list[dict],
    page: int = 0,
    total_pages: int = 1,
    guild_color: discord.Color | None = None,
) -> discord.Embed:
    """Build a summary embed for one page of calendar events."""
    color = guild_color or discord.Color.blurple()
    embed = discord.Embed(
        title=f"📆  {label} — Weekly Summary",
        color=color,
    )

    if not events:
        embed.description = "*No events scheduled for the next 7 days.*"
        return embed

    start = page * EVENTS_PER_PAGE
    end   = start + EVENTS_PER_PAGE
    page_events = events[start:end]

    for ev in page_events:
        value = f"🕐 {ev['time']}"
        if ev.get("location"):
            value += f"\n📍 {ev['location']}"
        embed.add_field(name=ev["title"], value=value, inline=False)

    if total_pages > 1:
        embed.set_footer(text=f"Page {page + 1} of {total_pages}")
    return embed


# ── Paginator view ────────────────────────────────────────────────────────────

class SummaryPaginatorView(discord.ui.View):
    """◀ / ▶ pagination for weekly summary embeds."""

    def __init__(self, label: str, events: list[dict], total_pages: int,
                 guild_color: discord.Color | None = None):
        super().__init__(timeout=300)
        self.label       = label
        self.events      = events
        self.total_pages = total_pages
        self.guild_color = guild_color
        self.page        = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.page -= 1
        self._update_buttons()
        embed = _build_summary_embed(self.label, self.events, self.page, self.total_pages, self.guild_color)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.page += 1
        self._update_buttons()
        embed = _build_summary_embed(self.label, self.events, self.page, self.total_pages, self.guild_color)
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ── Calendar picker view (shown after /gcalint verify) ───────────────────────

class CalendarPickerView(discord.ui.View):
    """
    Dropdown listing all calendars on the authenticated Google account.
    User selects which one to connect. Capped at 25 (Discord select limit).
    """

    def __init__(self, guild_id: int, integration_data: dict, calendars: list[dict]):
        super().__init__(timeout=120)
        self.guild_id         = guild_id
        self.integration_data = integration_data  # Everything except calendar_id

        options = []
        for cal in calendars[:25]:
            is_primary = cal.get("primary", False)
            emoji      = "⭐" if is_primary else "📅"
            options.append(discord.SelectOption(
                label=cal.get("summary", "Unnamed Calendar")[:100],
                value=cal["id"],
                emoji=emoji,
            ))

        select = discord.ui.Select(
            placeholder="Choose a calendar to connect...",
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        calendar_id   = interaction.data["values"][0]
        calendar_name = next(
            (o.label for o in self.children[0].options if o.value == calendar_id),
            calendar_id,
        )

        data = {**self.integration_data, "calendar_id": calendar_id}

        # Ensure guild_config row exists (FK constraint)
        with get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)",
                (self.guild_id,),
            )
            conn.execute(
                """
                INSERT INTO gcal_integrations
                    (guild_id, label, calendar_id, gcal_token, channel_id,
                     schedule, custom_interval, post_day, post_hour)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.guild_id,
                    data["label"],
                    data["calendar_id"],
                    data["gcal_token"],
                    data["channel_id"],
                    data["schedule"],
                    data.get("custom_interval", 7),
                    data.get("post_day", "monday"),
                    data.get("post_hour", 9),
                ),
            )
            conn.commit()

        log.info(
            f"gcalint: guild {self.guild_id} connected calendar "
            f"'{calendar_name}' ({calendar_id})"
        )

        self.stop()
        embed = discord.Embed(
            title="✅  Calendar Connected!",
            description=(
                f"**{data['label']}** is now linked to <#{data['channel_id']}> "
                f"and will post weekly summaries automatically.\n\n"
                f"Connected calendar: `{calendar_name}`\n\n"
                "Use `/gcalint post` to trigger a summary right now."
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=None)


# ── Setup wizard views ────────────────────────────────────────────────────────

class GcalIntSetupModal(discord.ui.Modal):
    """Step 3 of /gcalint add — collect label, channel ID, and post hour."""

    def __init__(self, guild_id: int, schedule: str, post_day: str,
                 custom_interval: int, *args, **kwargs):
        super().__init__(title="G-Cal Integration Setup", *args, **kwargs)
        self.guild_id        = guild_id
        self.schedule        = schedule
        self.post_day        = post_day
        self.custom_interval = custom_interval

        self.add_item(discord.ui.InputText(
            label="Integration Label",
            placeholder="e.g. TNC Events, MMA Schedule, Hockey",
            max_length=50,
        ))
        self.add_item(discord.ui.InputText(
            label="Channel ID",
            placeholder="Right-click your channel → Copy ID",
            max_length=20,
        ))
        self.add_item(discord.ui.InputText(
            label="Post Hour (0–23 UTC)",
            placeholder="e.g. 9 for 9am UTC",
            max_length=2,
        ))

    async def callback(self, interaction: discord.Interaction):
        label      = self.children[0].value.strip()
        channel_id_raw = self.children[1].value.strip()
        hour_raw   = self.children[2].value.strip()

        try:
            channel_id = int(channel_id_raw)
        except ValueError:
            await interaction.response.send_message(
                "❌ Invalid channel ID — must be a number. Right-click your channel and choose **Copy ID**.",
                ephemeral=True,
            )
            return

        try:
            post_hour = max(0, min(23, int(hour_raw)))
        except ValueError:
            post_hour = 9

        if not GCAL_AVAILABLE or not os.path.exists(CREDS_FILE):
            await interaction.response.send_message(
                "❌ Google Calendar libraries or credentials file not found.",
                ephemeral=True,
            )
            return

        flow = Flow.from_client_secrets_file(
            CREDS_FILE, scopes=SCOPES, redirect_uri="http://localhost"
        )
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

        # Store everything needed for verify step
        _pending_flows[self.guild_id] = {
            "flow":            flow,
            "label":           label,
            "channel_id":      channel_id,
            "schedule":        self.schedule,
            "post_day":        self.post_day,
            "custom_interval": self.custom_interval,
            "post_hour":       post_hour,
        }

        embed = discord.Embed(
            title="🔗  Authorize Google Calendar",
            description=(
                f"**Integration:** {label}\n"
                f"**Channel:** <#{channel_id}>\n\n"
                "**Steps:**\n"
                "1. Click the link below and sign in with Google.\n"
                "2. After authorizing, your browser will show **'This site can't be reached'** — that's normal!\n"
                "3. Copy the `code=` value from your browser's address bar.\n"
                "   *(The URL looks like: `http://localhost/?code=4/0Ab...&scope=...`)*\n"
                "4. Run `/gcalint verify <code>` and paste just the code.\n\n"
                f"[Click here to authorize]({auth_url})"
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class DaySelectView(discord.ui.View):
    """Step 2b of /gcalint add — pick the day of week (weekly schedule only)."""

    DAY_OPTIONS = [
        discord.SelectOption(label="Monday",    value="monday"),
        discord.SelectOption(label="Tuesday",   value="tuesday"),
        discord.SelectOption(label="Wednesday", value="wednesday"),
        discord.SelectOption(label="Thursday",  value="thursday"),
        discord.SelectOption(label="Friday",    value="friday"),
        discord.SelectOption(label="Saturday",  value="saturday"),
        discord.SelectOption(label="Sunday",    value="sunday"),
    ]

    def __init__(self, guild_id: int, author_id: int, schedule: str, custom_interval: int = 7):
        super().__init__(timeout=60)
        self.guild_id        = guild_id
        self.author_id       = author_id
        self.schedule        = schedule
        self.custom_interval = custom_interval

    @discord.ui.select(placeholder="Choose day of week...", options=DAY_OPTIONS)
    async def day_select(self, select: discord.ui.Select, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your setup wizard.", ephemeral=True)
            return
        self.stop()
        await interaction.response.send_modal(
            GcalIntSetupModal(
                guild_id=self.guild_id,
                schedule=self.schedule,
                post_day=select.values[0],
                custom_interval=self.custom_interval,
            )
        )


class ScheduleSelectView(discord.ui.View):
    """Step 1 of /gcalint add — pick posting schedule."""

    SCHEDULE_OPTIONS = [
        discord.SelectOption(label="Weekly",  value="weekly",  emoji="📅",
                             description="Post once a week on a chosen day"),
        discord.SelectOption(label="Daily",   value="daily",   emoji="🔁",
                             description="Post every day"),
        discord.SelectOption(label="Custom",  value="custom",  emoji="⚙️",
                             description="Set a custom interval in days"),
    ]

    def __init__(self, guild_id: int, author_id: int):
        super().__init__(timeout=60)
        self.guild_id  = guild_id
        self.author_id = author_id

    @discord.ui.select(placeholder="Choose posting schedule...", options=SCHEDULE_OPTIONS)
    async def schedule_select(self, select: discord.ui.Select, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your setup wizard.", ephemeral=True)
            return

        self.stop()
        value = select.values[0]

        if value == "weekly":
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="📅  G-Cal Integration — Step 2",
                    description="**Which day of the week should summaries be posted?**",
                    color=discord.Color.blurple(),
                ),
                view=DaySelectView(
                    guild_id=self.guild_id,
                    author_id=self.author_id,
                    schedule="weekly",
                ),
            )
        elif value == "daily":
            await interaction.response.send_modal(
                GcalIntSetupModal(
                    guild_id=self.guild_id,
                    schedule="daily",
                    post_day="",
                    custom_interval=1,
                )
            )
        else:
            # Custom — ask for interval via modal (reuse GcalIntSetupModal with custom flag)
            await interaction.response.send_modal(
                GcalIntSetupModal(
                    guild_id=self.guild_id,
                    schedule="custom",
                    post_day="",
                    custom_interval=7,
                )
            )


# ── Main posting helper ───────────────────────────────────────────────────────

async def _post_summary(bot: discord.Bot, integration: dict):
    """Fetch events and post the weekly summary embed for one integration."""
    guild_id   = integration["guild_id"]
    channel_id = integration["channel_id"]
    label      = integration["label"]
    token_json = integration["gcal_token"]
    cal_id     = integration["calendar_id"]
    int_id     = integration["id"]

    channel = bot.get_channel(channel_id)
    if not channel:
        log.warning(f"gcalint: channel {channel_id} not found for integration {int_id}")
        return

    # Fetch guild color
    from utils.database import get_guild_config
    from utils.embeds import get_guild_color
    cfg        = get_guild_config(guild_id)
    guild_color = get_guild_color(cfg.get("embed_color") if cfg else None)

    events      = await _fetch_week_events(token_json, cal_id)
    total_pages = ceil(len(events) / EVENTS_PER_PAGE) if events else 1
    embed       = _build_summary_embed(label, events, page=0, total_pages=total_pages, guild_color=guild_color)

    if total_pages > 1:
        view = SummaryPaginatorView(label, events, total_pages, guild_color=guild_color)
        await channel.send(embed=embed, view=view)
    else:
        await channel.send(embed=embed)

    with get_connection() as conn:
        conn.execute(
            "UPDATE gcal_integrations SET last_posted=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), int_id),
        )
        conn.commit()

    log.info(f"gcalint: posted summary for '{label}' (integration {int_id}) in guild {guild_id}")


# ── Cog ───────────────────────────────────────────────────────────────────────

class GcalIntegrations(commands.Cog):
    """Multi-calendar Google Calendar integration with auto-posting summaries."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.summary_loop.start()

    def cog_unload(self):
        self.summary_loop.cancel()

    gcalint = SlashCommandGroup("gcalint", "Google Calendar integration commands.")

    # ── /gcalint add ──────────────────────────────────────────────────────
    @gcalint.command(name="add", description="Connect a Google Calendar for auto-posting summaries.")
    @discord.default_permissions(administrator=True)
    async def gcalint_add(self, ctx: discord.ApplicationContext):
        guild_id = ctx.guild.id

        # Free tier cap
        if not is_premium(guild_id):
            with get_connection() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) as cnt FROM gcal_integrations WHERE guild_id=?",
                    (guild_id,),
                ).fetchone()["cnt"]
            if count >= FREE_GCAL_LIMIT:
                await ctx.respond(
                    embed=discord.Embed(
                        title="❌  Calendar Limit Reached",
                        description=(
                            f"Free servers can connect up to **{FREE_GCAL_LIMIT} calendars** "
                            f"({count}/{FREE_GCAL_LIMIT} used).\n\n"
                            "Upgrade to **[Soren Premium](https://soren.retrac.ca)** for unlimited integrations."
                        ),
                        color=discord.Color.red(),
                    ),
                    ephemeral=True,
                )
                return

        if not GCAL_AVAILABLE:
            await ctx.respond(
                embed=discord.Embed(
                    description="❌ Google Calendar libraries are not installed on this bot.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        if not os.path.exists(CREDS_FILE):
            await ctx.respond(
                embed=discord.Embed(
                    description=f"❌ Missing `{CREDS_FILE}`. See README for setup instructions.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📅  G-Cal Integration — Step 1",
            description="**How often should summaries be posted?**",
            color=discord.Color.blurple(),
        )
        await ctx.respond(
            embed=embed,
            view=ScheduleSelectView(guild_id=guild_id, author_id=ctx.author.id),
            ephemeral=True,
        )

    # ── /gcalint verify ───────────────────────────────────────────────────
    @gcalint.command(name="verify", description="Complete Google Calendar auth and pick a calendar.")
    @discord.default_permissions(administrator=True)
    async def gcalint_verify(
        self,
        ctx: discord.ApplicationContext,
        code: discord.Option(str, "The authorization code from the Google redirect URL."),
    ):
        pending = _pending_flows.pop(ctx.guild.id, None)
        if not pending:
            await ctx.respond(
                embed=discord.Embed(
                    description="❌ No pending connection. Run `/gcalint add` first.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        flow = pending["flow"]
        try:
            flow.fetch_token(code=code.strip())
            token_json = flow.credentials.to_json()
        except Exception as e:
            await ctx.respond(
                embed=discord.Embed(
                    description=f"❌ Failed to exchange auth code: `{e}`",
                    color=discord.Color.red(),
                ),
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
                embed=discord.Embed(
                    description=f"❌ Authorized but couldn't fetch calendar list: `{e}`",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        integration_data = {
            "label":           pending["label"],
            "channel_id":      pending["channel_id"],
            "schedule":        pending["schedule"],
            "post_day":        pending["post_day"],
            "custom_interval": pending["custom_interval"],
            "post_hour":       pending["post_hour"],
            "gcal_token":      token_json,
        }

        embed = discord.Embed(
            title="📅  Choose a Calendar",
            description=(
                "Authorization successful! Select which calendar to connect from the dropdown below.\n\n"
                "⭐ = Primary calendar"
            ),
            color=discord.Color.blurple(),
        )
        await ctx.respond(
            embed=embed,
            view=CalendarPickerView(
                guild_id=ctx.guild.id,
                integration_data=integration_data,
                calendars=calendars,
            ),
            ephemeral=True,
        )

    # ── /gcalint list ─────────────────────────────────────────────────────
    @gcalint.command(name="list", description="Show all connected Google Calendar integrations.")
    @discord.default_permissions(administrator=True)
    async def gcalint_list(self, ctx: discord.ApplicationContext):
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM gcal_integrations WHERE guild_id=? ORDER BY id ASC",
                (ctx.guild.id,),
            ).fetchall()

        if not rows:
            await ctx.respond(
                embed=discord.Embed(
                    description="No calendars connected yet. Use `/gcalint add` to connect one.",
                    color=discord.Color.blurple(),
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(title="📆  Connected Calendars", color=discord.Color.blurple())
        for row in rows:
            status     = "✅ Active" if row["active"] else "⏸️ Paused"
            channel    = f"<#{row['channel_id']}>"
            last_post  = row["last_posted"] or "Never"
            if row["schedule"] == "weekly":
                sched_str = f"Weekly on {row['post_day'].capitalize()} at {row['post_hour']:02d}:00 UTC"
            elif row["schedule"] == "daily":
                sched_str = f"Daily at {row['post_hour']:02d}:00 UTC"
            else:
                sched_str = f"Every {row['custom_interval']} days at {row['post_hour']:02d}:00 UTC"

            embed.add_field(
                name=f"[ID: {row['id']}]  {row['label']}  —  {status}",
                value=(
                    f"**Channel:** {channel}\n"
                    f"**Schedule:** {sched_str}\n"
                    f"**Last posted:** {last_post}"
                ),
                inline=False,
            )
        await ctx.respond(embed=embed, ephemeral=True)

    # ── /gcalint remove ───────────────────────────────────────────────────
    @gcalint.command(name="remove", description="Disconnect a Google Calendar integration.")
    @discord.default_permissions(administrator=True)
    async def gcalint_remove(
        self,
        ctx: discord.ApplicationContext,
        integration_id: discord.Option(int, "The integration ID to remove."),
    ):
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM gcal_integrations WHERE id=? AND guild_id=?",
                (integration_id, ctx.guild.id),
            ).fetchone()

        if not row:
            await ctx.respond(
                embed=discord.Embed(
                    description=f"❌ No integration found with ID `{integration_id}`.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        with get_connection() as conn:
            conn.execute("DELETE FROM gcal_integrations WHERE id=?", (integration_id,))
            conn.commit()

        await ctx.respond(
            embed=discord.Embed(
                description=f"✅ **{row['label']}** has been disconnected.",
                color=discord.Color.green(),
            ),
            ephemeral=True,
        )

    # ── /gcalint pause ────────────────────────────────────────────────────
    @gcalint.command(name="pause", description="Pause or resume a Google Calendar integration.")
    @discord.default_permissions(administrator=True)
    async def gcalint_pause(
        self,
        ctx: discord.ApplicationContext,
        integration_id: discord.Option(int, "The integration ID to pause or resume."),
    ):
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM gcal_integrations WHERE id=? AND guild_id=?",
                (integration_id, ctx.guild.id),
            ).fetchone()

        if not row:
            await ctx.respond(
                embed=discord.Embed(
                    description=f"❌ No integration found with ID `{integration_id}`.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        new_active = 0 if row["active"] else 1
        with get_connection() as conn:
            conn.execute(
                "UPDATE gcal_integrations SET active=? WHERE id=?",
                (new_active, integration_id),
            )
            conn.commit()

        state = "▶️ Resumed" if new_active else "⏸️ Paused"
        await ctx.respond(
            embed=discord.Embed(
                description=f"{state} **{row['label']}**.",
                color=discord.Color.green(),
            ),
            ephemeral=True,
        )

    # ── /gcalint post ─────────────────────────────────────────────────────
    @gcalint.command(name="post", description="Manually trigger a summary post for a calendar.")
    @discord.default_permissions(administrator=True)
    async def gcalint_post(
        self,
        ctx: discord.ApplicationContext,
        integration_id: discord.Option(int, "The integration ID to post now."),
    ):
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM gcal_integrations WHERE id=? AND guild_id=?",
                (integration_id, ctx.guild.id),
            ).fetchone()

        if not row:
            await ctx.respond(
                embed=discord.Embed(
                    description=f"❌ No integration found with ID `{integration_id}`.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        await ctx.defer(ephemeral=True)
        await _post_summary(self.bot, dict(row))
        await ctx.followup.send(
            embed=discord.Embed(
                description=f"✅ Summary posted for **{row['label']}**.",
                color=discord.Color.green(),
            ),
            ephemeral=True,
        )

    # ── Background posting loop ───────────────────────────────────────────
    @tasks.loop(minutes=5)
    async def summary_loop(self):
        """Check every 5 minutes if any integration is due for a post."""
        if not GCAL_AVAILABLE:
            return

        now = datetime.now(timezone.utc)

        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM gcal_integrations WHERE active=1"
            ).fetchall()

        for row in rows:
            integration = dict(row)
            try:
                if self._is_due(integration, now):
                    await _post_summary(self.bot, integration)
            except Exception as e:
                log.error(
                    f"gcalint: error posting for integration {integration['id']}: {e}"
                )

    @summary_loop.before_loop
    async def before_summary_loop(self):
        await self.bot.wait_until_ready()

    def _is_due(self, integration: dict, now: datetime) -> bool:
        """Return True if this integration should post right now."""
        post_hour = integration.get("post_hour", 9)
        if now.hour != post_hour:
            return False

        last_posted = integration.get("last_posted")
        schedule    = integration.get("schedule", "weekly")

        if not last_posted:
            return True  # Never posted — post now

        try:
            last_dt = datetime.fromisoformat(last_posted)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
        except Exception:
            return True

        elapsed_hours = (now - last_dt).total_seconds() / 3600

        if schedule == "daily":
            return elapsed_hours >= 20  # Allow some drift
        elif schedule == "weekly":
            day_name = now.strftime("%A").lower()
            return day_name == integration.get("post_day", "monday") and elapsed_hours >= 20
        elif schedule == "custom":
            interval_hours = integration.get("custom_interval", 7) * 24
            return elapsed_hours >= interval_hours - 4  # 4-hour tolerance
        return False


def setup(bot: discord.Bot):
    bot.add_cog(GcalIntegrations(bot))