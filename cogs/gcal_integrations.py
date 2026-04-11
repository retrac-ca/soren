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
PREMIUM_GCAL_LIMIT = 10

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

    def cog_unload(self):
        self.summary_loop.cancel()

    gcalint = SlashCommandGroup("gcalint", "Google Calendar integration commands.")

    # ── /gcalint add ──────────────────────────────────────────────────────
    @gcalint.command(name="add", description="Connect a Google Calendar for auto-posting summaries.")
    @discord.default_permissions(administrator=True)
    async def gcalint_add(self, ctx: discord.ApplicationContext):
        if not await _require_admin(ctx):
            return
        guild_id = ctx.guild.id

        if not is_premium(guild_id):
            with get_connection() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) as cnt FROM gcal_integrations WHERE guild_id=?", (guild_id,)
                ).fetchone()["cnt"]
            if count >= FREE_GCAL_LIMIT:
                await ctx.respond(
                    embed=discord.Embed(
                        title="❌  Calendar Limit Reached",
                        description=(
                            f"Free servers can connect up to **{FREE_GCAL_LIMIT} calendars** "
                            f"({count}/{FREE_GCAL_LIMIT} used).\n\n"
                            "Upgrade to **[Soren Premium](https://soren.retrac.ca)** for more integrations."
                        ),
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
            embed.add_field(
                name=f"[ID: {row['id']}]  {row['label']}  —  {status}",
                value=f"**Channel:** <#{row['channel_id']}>\n**Schedule:** {sched_str}\n**Last posted:** {row['last_posted'] or 'Never'}",
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