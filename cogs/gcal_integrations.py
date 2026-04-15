"""
cogs/gcal_integrations.py
==========================
Google Calendar Integrations — multi-calendar weekly digest summaries.

Each integration connects one Google Calendar to a Discord channel and
auto-posts a summary of upcoming events on a configurable schedule.
This is completely separate from the slash-command event system (/newevent).
No RSVP buttons — purely informational digest posts.

Commands (admin only)
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

EVENTS_PER_PAGE    = 8
FREE_GCAL_LIMIT    = 2
PREMIUM_GCAL_LIMIT = 5   # ← change this one number to raise the premium limit

# Pending OAuth flows keyed by guild_id
_pending_flows: dict[int, object] = {}


# ── Admin permission helper ───────────────────────────────────────────────────

async def _require_admin(ctx: discord.ApplicationContext) -> bool:
    """
    Belt-and-suspenders admin check for gcalint subcommands.
    @discord.default_permissions alone is not always enforced for subcommand
    groups in py-cord — this explicit check ensures non-admins are blocked.
    Returns True if the caller is an admin, False (after responding) if not.
    """
    if ctx.author.guild_permissions.administrator:
        return True
    await ctx.respond(
        embed=discord.Embed(
            description="❌ You need **Administrator** permission to use this command.",
            color=discord.Color.red(),
        ),
        ephemeral=True,
    )
    return False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_service(token_json: str):
    """Build an authenticated read-only Google Calendar service."""
    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    return gcal_build("calendar", "v3", credentials=creds)


def _clean_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r'<a[^>]*>(.*?)</a>', r'\1', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_module.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _format_event_time(item: dict) -> str:
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
    try:
        service  = _get_service(token_json)
        now      = datetime.now(timezone.utc)
        week_end = now + timedelta(days=7)
        result   = service.events().list(
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


async def _fetch_upcoming_for_reminders(token_json: str, calendar_id: str,
                                         lookahead_minutes: int) -> list[dict]:
    """
    Fetch events starting within the next `lookahead_minutes` minutes.
    Returns full event data including gcal_event_id, start_dt, description.
    Used exclusively by the reminder loop.
    """
    try:
        service   = _get_service(token_json)
        now       = datetime.now(timezone.utc)
        look_end  = now + timedelta(minutes=lookahead_minutes + 5)  # small buffer

        result = service.events().list(
            calendarId=calendar_id,
            timeMin=now.isoformat(),
            timeMax=look_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        ).execute()

        events = []
        for item in result.get("items", []):
            start = item.get("start", {})
            start_str = start.get("dateTime") or start.get("date", "")
            try:
                if "T" in start_str:
                    start_dt = datetime.fromisoformat(start_str)
                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=timezone.utc)
                    else:
                        start_dt = start_dt.astimezone(timezone.utc)
                else:
                    # All-day event — skip, no meaningful reminder time
                    continue
            except Exception:
                continue

            events.append({
                "gcal_event_id": item.get("id", ""),
                "title":         item.get("summary", "Untitled"),
                "start_dt":      start_dt,
                "time_str":      _format_event_time(item),
                "location":      _clean_html(item.get("location", "")),
                "description":   _clean_html(item.get("description", "")),
            })
        return events
    except Exception as e:
        log.error(f"_fetch_upcoming_for_reminders failed for calendar {calendar_id}: {e}")
        return []


def _build_summary_embed(
    label: str,
    events: list[dict],
    page: int = 0,
    total_pages: int = 1,
    guild_color: discord.Color | None = None,
) -> discord.Embed:
    color = guild_color or discord.Color.blurple()
    embed = discord.Embed(title=f"📆  {label} — Weekly Summary", color=color)
    if not events:
        embed.description = "*No events scheduled for the next 7 days.*"
        return embed
    start = page * EVENTS_PER_PAGE
    for ev in events[start:start + EVENTS_PER_PAGE]:
        value = f"🕐 {ev['time']}"
        if ev.get("location"):
            value += f"\n📍 {ev['location']}"
        embed.add_field(name=ev["title"], value=value, inline=False)
    if total_pages > 1:
        embed.set_footer(text=f"Page {page + 1} of {total_pages}")
    return embed


# ── Paginator view ────────────────────────────────────────────────────────────

class SummaryPaginatorView(discord.ui.View):
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
        await interaction.response.edit_message(
            embed=_build_summary_embed(self.label, self.events, self.page, self.total_pages, self.guild_color),
            view=self,
        )

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(
            embed=_build_summary_embed(self.label, self.events, self.page, self.total_pages, self.guild_color),
            view=self,
        )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ── Calendar picker view ──────────────────────────────────────────────────────

class CalendarPickerView(discord.ui.View):
    def __init__(self, guild_id: int, integration_data: dict, calendars: list[dict]):
        super().__init__(timeout=120)
        self.guild_id         = guild_id
        self.integration_data = integration_data
        options = []
        for cal in calendars[:25]:
            emoji = "⭐" if cal.get("primary", False) else "📅"
            options.append(discord.SelectOption(
                label=cal.get("summary", "Unnamed Calendar")[:100],
                value=cal["id"],
                emoji=emoji,
            ))
        select = discord.ui.Select(placeholder="Choose a calendar to connect...", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        calendar_id   = interaction.data["values"][0]
        calendar_name = next(
            (o.label for o in self.children[0].options if o.value == calendar_id),
            calendar_id,
        )
        data = {**self.integration_data, "calendar_id": calendar_id}
        with get_connection() as conn:
            conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (self.guild_id,))
            conn.execute(
                """
                INSERT INTO gcal_integrations
                    (guild_id, label, calendar_id, gcal_token, channel_id,
                     schedule, custom_interval, post_day, post_hour)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.guild_id, data["label"], data["calendar_id"],
                    data["gcal_token"], data["channel_id"], data["schedule"],
                    data.get("custom_interval", 7), data.get("post_day", "monday"),
                    data.get("post_hour", 9),
                ),
            )
            conn.commit()
        log.info(f"gcalint: guild {self.guild_id} connected calendar '{calendar_name}' ({calendar_id})")
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅  Calendar Connected!",
                description=(
                    f"**{data['label']}** is now linked to <#{data['channel_id']}> "
                    f"and will post summaries automatically.\n\n"
                    f"Connected calendar: `{calendar_name}`\n\n"
                    "Use `/gcalint post` to trigger a summary right now."
                ),
                color=discord.Color.green(),
            ),
            view=None,
        )


# ── Setup wizard views ────────────────────────────────────────────────────────

class GcalIntSetupModal(discord.ui.Modal):
    def __init__(self, guild_id: int, schedule: str, post_day: str,
                 custom_interval: int, *args, **kwargs):
        super().__init__(title="G-Cal Integration Setup", *args, **kwargs)
        self.guild_id        = guild_id
        self.schedule        = schedule
        self.post_day        = post_day
        self.custom_interval = custom_interval
        self.add_item(discord.ui.InputText(label="Integration Label", placeholder="e.g. TNC Events, MMA Schedule", max_length=50))
        self.add_item(discord.ui.InputText(label="Channel ID", placeholder="Right-click your channel → Copy ID", max_length=20))
        self.add_item(discord.ui.InputText(label="Post Hour (0–23 UTC)", placeholder="e.g. 9 for 9am UTC", max_length=2))

    async def callback(self, interaction: discord.Interaction):
        label      = self.children[0].value.strip()
        hour_raw   = self.children[2].value.strip()
        try:
            channel_id = int(self.children[1].value.strip())
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
                "❌ Google Calendar libraries or credentials file not found.", ephemeral=True,
            )
            return
        flow = Flow.from_client_secrets_file(CREDS_FILE, scopes=SCOPES, redirect_uri="http://localhost")
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
        _pending_flows[self.guild_id] = {
            "flow": flow, "label": label, "channel_id": channel_id,
            "schedule": self.schedule, "post_day": self.post_day,
            "custom_interval": self.custom_interval, "post_hour": post_hour,
        }
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🔗  Authorize Google Calendar",
                description=(
                    f"**Integration:** {label}\n**Channel:** <#{channel_id}>\n\n"
                    "**Steps:**\n"
                    "1. Click the link below and sign in with Google.\n"
                    "2. After authorizing, your browser will show **'This site can't be reached'** — that's normal!\n"
                    "3. Copy the `code=` value from your browser's address bar.\n"
                    "   *(The URL looks like: `http://localhost/?code=4/0Ab...&scope=...`)*\n"
                    "4. Run `/gcalint verify <code>` and paste just the code.\n\n"
                    f"[Click here to authorize]({auth_url})"
                ),
                color=discord.Color.blurple(),
            ),
            ephemeral=True,
        )


class DaySelectView(discord.ui.View):
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
        self.guild_id = guild_id; self.author_id = author_id
        self.schedule = schedule; self.custom_interval = custom_interval

    @discord.ui.select(placeholder="Choose day of week...", options=DAY_OPTIONS)
    async def day_select(self, select: discord.ui.Select, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your setup wizard.", ephemeral=True)
            return
        self.stop()
        await interaction.response.send_modal(
            GcalIntSetupModal(guild_id=self.guild_id, schedule=self.schedule,
                              post_day=select.values[0], custom_interval=self.custom_interval)
        )


class ScheduleSelectView(discord.ui.View):
    SCHEDULE_OPTIONS = [
        discord.SelectOption(label="Weekly",  value="weekly",  emoji="📅", description="Post once a week on a chosen day"),
        discord.SelectOption(label="Daily",   value="daily",   emoji="🔁", description="Post every day"),
        discord.SelectOption(label="Custom",  value="custom",  emoji="⚙️", description="Set a custom interval in days"),
    ]

    def __init__(self, guild_id: int, author_id: int):
        super().__init__(timeout=60)
        self.guild_id = guild_id; self.author_id = author_id

    @discord.ui.select(placeholder="Choose posting schedule...", options=SCHEDULE_OPTIONS)
    async def schedule_select(self, select: discord.ui.Select, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your setup wizard.", ephemeral=True)
            return
        self.stop()
        value = select.values[0]
        if value == "weekly":
            await interaction.response.edit_message(
                embed=discord.Embed(title="📅  G-Cal Integration — Step 2",
                                    description="**Which day of the week should summaries be posted?**",
                                    color=discord.Color.blurple()),
                view=DaySelectView(guild_id=self.guild_id, author_id=self.author_id, schedule="weekly"),
            )
        elif value == "daily":
            await interaction.response.send_modal(
                GcalIntSetupModal(guild_id=self.guild_id, schedule="daily", post_day="", custom_interval=1)
            )
        else:
            await interaction.response.send_modal(
                GcalIntSetupModal(guild_id=self.guild_id, schedule="custom", post_day="", custom_interval=7)
            )


# ── Main posting helper ───────────────────────────────────────────────────────

async def _post_summary(bot: discord.Bot, integration: dict):
    guild_id   = integration["guild_id"]
    channel_id = integration["channel_id"]
    label      = integration["label"]
    int_id     = integration["id"]

    channel = bot.get_channel(channel_id)
    if not channel:
        log.warning(f"gcalint: channel {channel_id} not found for integration {int_id}")
        return

    from utils.database import get_guild_config
    from utils.embeds import get_guild_color
    cfg         = get_guild_config(guild_id)
    guild_color = get_guild_color(cfg.get("embed_color") if cfg else None)

    events      = await _fetch_week_events(integration["gcal_token"], integration["calendar_id"])
    total_pages = ceil(len(events) / EVENTS_PER_PAGE) if events else 1
    embed       = _build_summary_embed(label, events, page=0, total_pages=total_pages, guild_color=guild_color)

    if total_pages > 1:
        await channel.send(embed=embed, view=SummaryPaginatorView(label, events, total_pages, guild_color=guild_color))
    else:
        await channel.send(embed=embed)

    with get_connection() as conn:
        conn.execute("UPDATE gcal_integrations SET last_posted=? WHERE id=?",
                     (datetime.now(timezone.utc).isoformat(), int_id))
        conn.commit()
    log.info(f"gcalint: posted summary for '{label}' (integration {int_id}) in guild {guild_id}")


# ── Cog ───────────────────────────────────────────────────────────────────────

class GcalIntegrations(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.summary_loop.start()
        self.reminder_loop.start()

    def cog_unload(self):
        self.summary_loop.cancel()
        self.reminder_loop.cancel()

    gcalint = SlashCommandGroup("gcalint", "Google Calendar integration commands.")

    # ── /gcalint add ──────────────────────────────────────────────────────
    @gcalint.command(name="add", description="Connect a Google Calendar for auto-posting summaries.")
    @discord.default_permissions(administrator=True)
    async def gcalint_add(self, ctx: discord.ApplicationContext):
        if not await _require_admin(ctx):
            return
        guild_id = ctx.guild.id

        premium = is_premium(guild_id)
        limit   = PREMIUM_GCAL_LIMIT if premium else FREE_GCAL_LIMIT

        with get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) as cnt FROM gcal_integrations WHERE guild_id=?", (guild_id,)
            ).fetchone()["cnt"]

        if count >= limit:
            if premium:
                description = (
                    f"Premium servers can connect up to **{PREMIUM_GCAL_LIMIT} calendars** "
                    f"({count}/{PREMIUM_GCAL_LIMIT} used).\n\n"
                    "Please remove an existing integration to add a new one."
                )
            else:
                description = (
                    f"Free servers can connect up to **{FREE_GCAL_LIMIT} calendars** "
                    f"({count}/{FREE_GCAL_LIMIT} used).\n\n"
                    "Upgrade to **[Soren Premium](https://soren.retrac.ca)** for up to "
                    f"{PREMIUM_GCAL_LIMIT} integrations."
                )
            await ctx.respond(
                embed=discord.Embed(
                    title="❌  Calendar Limit Reached",
                    description=description,
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        if not GCAL_AVAILABLE:
            await ctx.respond(embed=discord.Embed(description="❌ Google Calendar libraries are not installed.", color=discord.Color.red()), ephemeral=True)
            return
        if not os.path.exists(CREDS_FILE):
            await ctx.respond(embed=discord.Embed(description=f"❌ Missing `{CREDS_FILE}`.", color=discord.Color.red()), ephemeral=True)
            return

        await ctx.respond(
            embed=discord.Embed(title="📅  G-Cal Integration — Step 1", description="**How often should summaries be posted?**", color=discord.Color.blurple()),
            view=ScheduleSelectView(guild_id=guild_id, author_id=ctx.author.id),
            ephemeral=True,
        )

    # ── /gcalint verify ───────────────────────────────────────────────────
    @gcalint.command(name="verify", description="Complete Google Calendar auth and pick a calendar.")
    @discord.default_permissions(administrator=True)
    async def gcalint_verify(self, ctx: discord.ApplicationContext,
                              code: discord.Option(str, "The authorization code from the Google redirect URL.")):
        if not await _require_admin(ctx):
            return
        pending = _pending_flows.pop(ctx.guild.id, None)
        if not pending:
            await ctx.respond(embed=discord.Embed(description="❌ No pending connection. Run `/gcalint add` first.", color=discord.Color.red()), ephemeral=True)
            return
        try:
            pending["flow"].fetch_token(code=code.strip())
            token_json = pending["flow"].credentials.to_json()
        except Exception as e:
            await ctx.respond(embed=discord.Embed(description=f"❌ Failed to exchange auth code: `{e}`", color=discord.Color.red()), ephemeral=True)
            return
        try:
            service   = _get_service(token_json)
            calendars = service.calendarList().list().execute().get("items", [])
        except Exception as e:
            await ctx.respond(embed=discord.Embed(description=f"❌ Authorized but couldn't fetch calendar list: `{e}`", color=discord.Color.red()), ephemeral=True)
            return

        integration_data = {
            "label": pending["label"], "channel_id": pending["channel_id"],
            "schedule": pending["schedule"], "post_day": pending["post_day"],
            "custom_interval": pending["custom_interval"], "post_hour": pending["post_hour"],
            "gcal_token": token_json,
        }
        await ctx.respond(
            embed=discord.Embed(title="📅  Choose a Calendar", description="Authorization successful! Select which calendar to connect.\n\n⭐ = Primary calendar", color=discord.Color.blurple()),
            view=CalendarPickerView(guild_id=ctx.guild.id, integration_data=integration_data, calendars=calendars),
            ephemeral=True,
        )

    # ── /gcalint list ─────────────────────────────────────────────────────
    @gcalint.command(name="list", description="Show all connected Google Calendar integrations.")
    @discord.default_permissions(administrator=True)
    async def gcalint_list(self, ctx: discord.ApplicationContext):
        if not await _require_admin(ctx):
            return
        with get_connection() as conn:
            rows = conn.execute("SELECT * FROM gcal_integrations WHERE guild_id=? ORDER BY id ASC", (ctx.guild.id,)).fetchall()
        if not rows:
            await ctx.respond(embed=discord.Embed(description="No calendars connected yet. Use `/gcalint add` to connect one.", color=discord.Color.blurple()), ephemeral=True)
            return
        embed = discord.Embed(title="📆  Connected Calendars", color=discord.Color.blurple())
        for row in rows:
            status = "✅ Active" if row["active"] else "⏸️ Paused"
            if row["schedule"] == "weekly":
                sched_str = f"Weekly on {row['post_day'].capitalize()} at {row['post_hour']:02d}:00 UTC"
            elif row["schedule"] == "daily":
                sched_str = f"Daily at {row['post_hour']:02d}:00 UTC"
            else:
                sched_str = f"Every {row['custom_interval']} days at {row['post_hour']:02d}:00 UTC"
            # Reminder info
            reminders_on = row["reminders_enabled"] if "reminders_enabled" in row.keys() else 1
            offset       = row["reminder_offset"]   if "reminder_offset"   in row.keys() else 15
            offset_str   = {15: "15 min", 30: "30 min", 60: "1 hour", 1440: "1 day"}.get(offset, f"{offset} min")
            reminder_str = f"✅ {offset_str} before" if reminders_on else "⏸️ Disabled"
            embed.add_field(
                name=f"[ID: {row['id']}]  {row['label']}  —  {status}",
                value=(
                    f"**Channel:** <#{row['channel_id']}>\n"
                    f"**Schedule:** {sched_str}\n"
                    f"**Reminders:** {reminder_str}\n"
                    f"**Last posted:** {row['last_posted'] or 'Never'}"
                ),
                inline=False,
            )
        await ctx.respond(embed=embed, ephemeral=True)

    # ── /gcalint remove ───────────────────────────────────────────────────
    @gcalint.command(name="remove", description="Disconnect a Google Calendar integration.")
    @discord.default_permissions(administrator=True)
    async def gcalint_remove(self, ctx: discord.ApplicationContext,
                              integration_id: discord.Option(int, "The integration ID to remove.")):
        if not await _require_admin(ctx):
            return
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM gcal_integrations WHERE id=? AND guild_id=?", (integration_id, ctx.guild.id)).fetchone()
        if not row:
            await ctx.respond(embed=discord.Embed(description=f"❌ No integration found with ID `{integration_id}`.", color=discord.Color.red()), ephemeral=True)
            return
        with get_connection() as conn:
            conn.execute("DELETE FROM gcal_integrations WHERE id=?", (integration_id,))
            conn.commit()
        await ctx.respond(embed=discord.Embed(description=f"✅ **{row['label']}** has been disconnected.", color=discord.Color.green()), ephemeral=True)

    # ── /gcalint pause ────────────────────────────────────────────────────
    @gcalint.command(name="pause", description="Pause or resume a Google Calendar integration.")
    @discord.default_permissions(administrator=True)
    async def gcalint_pause(self, ctx: discord.ApplicationContext,
                             integration_id: discord.Option(int, "The integration ID to pause or resume.")):
        if not await _require_admin(ctx):
            return
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM gcal_integrations WHERE id=? AND guild_id=?", (integration_id, ctx.guild.id)).fetchone()
        if not row:
            await ctx.respond(embed=discord.Embed(description=f"❌ No integration found with ID `{integration_id}`.", color=discord.Color.red()), ephemeral=True)
            return
        new_active = 0 if row["active"] else 1
        with get_connection() as conn:
            conn.execute("UPDATE gcal_integrations SET active=? WHERE id=?", (new_active, integration_id))
            conn.commit()
        state = "▶️ Resumed" if new_active else "⏸️ Paused"
        await ctx.respond(embed=discord.Embed(description=f"{state} **{row['label']}**.", color=discord.Color.green()), ephemeral=True)

    # ── /gcalint post ─────────────────────────────────────────────────────
    @gcalint.command(name="post", description="Manually trigger a summary post for a calendar.")
    @discord.default_permissions(administrator=True)
    async def gcalint_post(self, ctx: discord.ApplicationContext,
                            integration_id: discord.Option(int, "The integration ID to post now.")):
        if not await _require_admin(ctx):
            return
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM gcal_integrations WHERE id=? AND guild_id=?", (integration_id, ctx.guild.id)).fetchone()
        if not row:
            await ctx.respond(embed=discord.Embed(description=f"❌ No integration found with ID `{integration_id}`.", color=discord.Color.red()), ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        await _post_summary(self.bot, dict(row))
        await ctx.followup.send(embed=discord.Embed(description=f"✅ Summary posted for **{row['label']}**.", color=discord.Color.green()), ephemeral=True)

    # ── /gcalint reminder ─────────────────────────────────────────────────
    @gcalint.command(name="reminder", description="Set how far in advance to send event reminders for an integration.")
    @discord.default_permissions(administrator=True)
    async def gcalint_reminder(
        self,
        ctx: discord.ApplicationContext,
        integration_id: discord.Option(int, "The integration ID to configure."),
        offset: discord.Option(
            str,
            "How far in advance to remind.",
            choices=[
                discord.OptionChoice(name="15 minutes before", value="15"),
                discord.OptionChoice(name="30 minutes before", value="30"),
                discord.OptionChoice(name="1 hour before",     value="60"),
                discord.OptionChoice(name="1 day before",      value="1440"),
            ],
        ),
    ):
        if not await _require_admin(ctx):
            return
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM gcal_integrations WHERE id=? AND guild_id=?", (integration_id, ctx.guild.id)).fetchone()
        if not row:
            await ctx.respond(embed=discord.Embed(description=f"❌ No integration found with ID `{integration_id}`.", color=discord.Color.red()), ephemeral=True)
            return
        offset_int = int(offset)
        with get_connection() as conn:
            conn.execute("UPDATE gcal_integrations SET reminder_offset=? WHERE id=?", (offset_int, integration_id))
            conn.commit()
        label_map = {"15": "15 minutes", "30": "30 minutes", "60": "1 hour", "1440": "1 day"}
        await ctx.respond(
            embed=discord.Embed(
                description=f"✅ **{row['label']}** reminders will now fire **{label_map[offset]} before** each event.",
                color=discord.Color.green(),
            ),
            ephemeral=True,
        )
        log.info(f"gcalint: guild {ctx.guild.id} set reminder_offset={offset_int} for integration {integration_id}")

    # ── /gcalint reminders ────────────────────────────────────────────────
    @gcalint.command(name="reminders", description="Toggle event reminders on or off for an integration.")
    @discord.default_permissions(administrator=True)
    async def gcalint_reminders(
        self,
        ctx: discord.ApplicationContext,
        integration_id: discord.Option(int, "The integration ID to toggle reminders for."),
    ):
        if not await _require_admin(ctx):
            return
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM gcal_integrations WHERE id=? AND guild_id=?", (integration_id, ctx.guild.id)).fetchone()
        if not row:
            await ctx.respond(embed=discord.Embed(description=f"❌ No integration found with ID `{integration_id}`.", color=discord.Color.red()), ephemeral=True)
            return
        current = row["reminders_enabled"] if "reminders_enabled" in row.keys() else 1
        new_val = 0 if current else 1
        with get_connection() as conn:
            conn.execute("UPDATE gcal_integrations SET reminders_enabled=? WHERE id=?", (new_val, integration_id))
            conn.commit()
        state = "✅ Enabled" if new_val else "⏸️ Disabled"
        await ctx.respond(
            embed=discord.Embed(
                description=f"{state} reminders for **{row['label']}**.",
                color=discord.Color.green(),
            ),
            ephemeral=True,
        )
        log.info(f"gcalint: guild {ctx.guild.id} set reminders_enabled={new_val} for integration {integration_id}")

    # ── Background loop ───────────────────────────────────────────────────
    @tasks.loop(minutes=5)
    async def summary_loop(self):
        if not GCAL_AVAILABLE:
            return
        now = datetime.now(timezone.utc)
        with get_connection() as conn:
            rows = conn.execute("SELECT * FROM gcal_integrations WHERE active=1").fetchall()
        for row in rows:
            integration = dict(row)
            try:
                if self._is_due(integration, now):
                    await _post_summary(self.bot, integration)
            except Exception as e:
                log.error(f"gcalint: error posting for integration {integration['id']}: {e}")

    @summary_loop.before_loop
    async def before_summary_loop(self):
        await self.bot.wait_until_ready()

    # ── Reminder loop ─────────────────────────────────────────────────────
    @tasks.loop(minutes=5)
    async def reminder_loop(self):
        """
        Runs every 5 minutes. For each active integration with reminders enabled,
        fetches upcoming events and fires a reminder message if:
          - The event starts within the reminder window
          - The event hasn't already been reminded (tracked in gcal_reminders)

        Optimization: for short offsets (< 60 min), we only call the GCal API
        during the minutes-of-hour window where a reminder could plausibly fire.
        For example, a 15-min offset only needs to fetch between :00 and :20 of
        each hour since any event starting in that hour will be caught. For
        longer offsets (1 day) we always fetch since the window is too large to
        skip safely.
        """
        if not GCAL_AVAILABLE:
            return

        now = datetime.now(timezone.utc)

        # ── Cleanup: remove reminder records older than 30 days ───────────
        try:
            cutoff = (now - timedelta(days=30)).isoformat()
            with get_connection() as conn:
                conn.execute(
                    "DELETE FROM gcal_reminders WHERE fired_at IS NOT NULL AND fired_at < ?",
                    (cutoff,),
                )
                conn.commit()
        except Exception as e:
            log.warning(f"gcalint reminder: cleanup failed: {e}")

        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM gcal_integrations WHERE active=1 AND reminders_enabled=1"
            ).fetchall()

        for row in rows:
            integration = dict(row)
            int_id      = integration["id"]
            offset      = integration.get("reminder_offset", 15)

            # ── API call optimization ─────────────────────────────────────
            # For offsets under 60 minutes, only fetch during the window of
            # the current hour where a reminder could fire. The loop runs every
            # 5 minutes so we add a 10-minute buffer to avoid missing edge cases.
            # For 1-day offsets, always fetch — the window spans the whole day.
            if offset < 60:
                # Current minute within the hour (0–59)
                current_minute = now.minute
                # A reminder fires when: event_start - offset <= now <= event_start
                # So we only need to fetch if now is within [0, offset+10] minutes
                # of any possible event start. Since events start at any minute,
                # we can't skip based on the hour alone — but we CAN skip if the
                # loop has already run within the last tick and found nothing.
                # Practical optimization: skip if current_minute > (offset + 10)
                # AND current_minute < (60 - 10), meaning we're in the "dead zone"
                # of the hour where no short-offset reminders would fire for events
                # starting at :00 of the next hour.
                dead_zone_start = offset + 10
                dead_zone_end   = 50  # 10 min before the hour
                if dead_zone_start < dead_zone_end and dead_zone_start <= current_minute <= dead_zone_end:
                    log.debug(f"gcalint reminder: skipping API call for integration {int_id} (minute {current_minute} in dead zone {dead_zone_start}–{dead_zone_end})")
                    continue

            try:
                events = await _fetch_upcoming_for_reminders(
                    integration["gcal_token"],
                    integration["calendar_id"],
                    lookahead_minutes=offset,
                )
            except Exception as e:
                log.error(f"gcalint reminder: failed to fetch events for integration {int_id}: {e}")
                continue

            for event in events:
                gcal_event_id = event["gcal_event_id"]
                if not gcal_event_id:
                    continue

                start_dt = event["start_dt"]
                diff     = (start_dt - now).total_seconds() / 60  # minutes until start

                # Only fire if within the reminder window (with 5-min loop tolerance)
                if not (0 <= diff <= offset + 5):
                    continue

                # Check if already reminded
                with get_connection() as conn:
                    already = conn.execute(
                        "SELECT id FROM gcal_reminders WHERE integration_id=? AND gcal_event_id=?",
                        (int_id, gcal_event_id),
                    ).fetchone()
                if already:
                    continue

                # Mark as reminded immediately to prevent double-sends
                try:
                    with get_connection() as conn:
                        conn.execute(
                            "INSERT OR IGNORE INTO gcal_reminders (integration_id, gcal_event_id, fired_at) VALUES (?, ?, ?)",
                            (int_id, gcal_event_id, now.isoformat()),
                        )
                        conn.commit()
                except Exception as e:
                    log.error(f"gcalint reminder: failed to record reminder for {gcal_event_id}: {e}")
                    continue

                # Send the reminder
                channel = self.bot.get_channel(integration["channel_id"])
                if not channel:
                    log.warning(f"gcalint reminder: channel {integration['channel_id']} not found for integration {int_id}")
                    continue

                # Build reminder embed
                from utils.database import get_guild_config
                from utils.embeds import get_guild_color
                cfg         = get_guild_config(integration["guild_id"])
                guild_color = get_guild_color(cfg.get("embed_color") if cfg else None)

                offset_label = {15: "15 minutes", 30: "30 minutes", 60: "1 hour", 1440: "1 day"}.get(offset, f"{offset} minutes")
                description  = f"**{event['title']}** is starting in **{offset_label}**."
                if event.get("time_str"):
                    description += f"\n🕐 {event['time_str']}"
                if event.get("location"):
                    description += f"\n📍 {event['location']}"
                if event.get("description"):
                    # Truncate long descriptions
                    desc_text = event["description"][:300]
                    if len(event["description"]) > 300:
                        desc_text += "…"
                    description += f"\n\n{desc_text}"

                embed = discord.Embed(
                    title=f"⏰  {event['title']} is starting soon!",
                    description=description,
                    color=guild_color,
                )
                embed.set_footer(text=f"From: {integration['label']}")

                try:
                    await channel.send(embed=embed)
                    log.info(f"gcalint reminder: sent for '{event['title']}' (integration {int_id})")
                except discord.Forbidden:
                    log.warning(f"gcalint reminder: no permission in channel {integration['channel_id']}")
                except Exception as e:
                    log.error(f"gcalint reminder: unexpected error for integration {int_id}: {e}")

    @reminder_loop.before_loop
    async def before_reminder_loop(self):
        await self.bot.wait_until_ready()

    def _is_due(self, integration: dict, now: datetime) -> bool:
        if now.hour != integration.get("post_hour", 9):
            return False
        last_posted = integration.get("last_posted")
        schedule    = integration.get("schedule", "weekly")
        if not last_posted:
            return True
        try:
            last_dt = datetime.fromisoformat(last_posted)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
        except Exception:
            return True
        elapsed_hours = (now - last_dt).total_seconds() / 3600
        if schedule == "daily":
            return elapsed_hours >= 20
        elif schedule == "weekly":
            return now.strftime("%A").lower() == integration.get("post_day", "monday") and elapsed_hours >= 20
        elif schedule == "custom":
            return elapsed_hours >= integration.get("custom_interval", 7) * 24 - 4
        return False


def setup(bot: discord.Bot):
    bot.add_cog(GcalIntegrations(bot))