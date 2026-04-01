"""
cogs/events.py
===============
Slash commands for creating, editing, and deleting events.

What's new in this version
---------------------------
- Recurring event support: daily, weekly, bi-weekly, bi-monthly, monthly, custom
- Two-step /newevent flow:
    Step 1 — Select menu to choose Single or Recurring type
    Step 2 — Modal form for all event details
- EditEventModal is now also triggered by the Edit button on the embed
  (the button itself lives in cogs/rsvp.py inside EventView)

Commands
--------
/newevent    — Two-step event creation (type select → detail modal)
/editevent   — Edit an existing event by ID (slash command fallback)
/deleteevent — Delete an event by ID
/listevents  — List all upcoming events in this server
"""

import discord
from discord.ext import commands
from datetime import datetime, timedelta
import pytz

from utils.database import get_connection, is_premium, get_guild_config
from utils.permissions import is_event_creator, check_setup
from utils.embeds import build_event_embed, build_error_embed, build_success_embed, COLOR_EVENT
import logging

try:
    import dateparser
    HAS_DATEPARSER = True
except ImportError:
    HAS_DATEPARSER = False

log = logging.getLogger("soren.events")


def _parse_datetime(raw: str, tz_name: str) -> datetime | None:
    """
    Parse a date/time string in any reasonable format.
    Tries strict YYYY-MM-DD HH:MM first, then falls back to dateparser
    for natural language input like "July 4 8pm" or "next Friday 9pm".
    Returns a timezone-aware datetime or None if parsing fails.
    """
    import pytz
    tz = pytz.timezone(tz_name)

    # Try strict format first
    try:
        return tz.localize(datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M"))
    except ValueError:
        pass

    # Fall back to dateparser for natural language
    if HAS_DATEPARSER:
        settings = {
            "TIMEZONE": tz_name,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
            "DATE_ORDER": "MDY",
        }
        parsed = dateparser.parse(raw.strip(), settings=settings)
        if parsed:
            # Convert to the event's timezone
            return parsed.astimezone(tz)

    return None

# ── Try to import relativedelta for monthly math ──────────────────────────────
try:
    from dateutil.relativedelta import relativedelta
    HAS_DATEUTIL = True
except ImportError:
    HAS_DATEUTIL = False

# ── Free tier limit ───────────────────────────────────────────────────────────
FREE_EVENT_LIMIT = 10

# ── North American timezone options ──────────────────────────────────────────
NA_TIMEZONES = [
    discord.SelectOption(label="Eastern Time (ET)",          value="America/New_York",       emoji="🕐", description="UTC-5 / UTC-4 (DST)"),
    discord.SelectOption(label="Central Time (CT)",          value="America/Chicago",         emoji="🕐", description="UTC-6 / UTC-5 (DST)"),
    discord.SelectOption(label="Mountain Time (MT)",         value="America/Denver",          emoji="🕐", description="UTC-7 / UTC-6 (DST)"),
    discord.SelectOption(label="Mountain Time - AZ (no DST)",value="America/Phoenix",         emoji="🕐", description="UTC-7, no daylight saving"),
    discord.SelectOption(label="Pacific Time (PT)",          value="America/Los_Angeles",     emoji="🕐", description="UTC-8 / UTC-7 (DST)"),
    discord.SelectOption(label="Alaska Time (AKT)",          value="America/Anchorage",       emoji="🕐", description="UTC-9 / UTC-8 (DST)"),
    discord.SelectOption(label="Hawaii Time (HT)",           value="Pacific/Honolulu",        emoji="🕐", description="UTC-10, no daylight saving"),
    discord.SelectOption(label="Atlantic Time (AT)",         value="America/Halifax",         emoji="🕐", description="UTC-4 / UTC-3 (DST)"),
    discord.SelectOption(label="Newfoundland Time (NT)",     value="America/St_Johns",        emoji="🕐", description="UTC-3:30 / UTC-2:30 (DST)"),
    discord.SelectOption(label="UTC",                        value="UTC",                     emoji="🌐", description="Coordinated Universal Time"),
]

# ── Reminder offset options ──────────────────────────────────────────────────
REMINDER_OPTIONS = [
    discord.SelectOption(label="15 minutes before", value="15",     emoji="⏰"),
    discord.SelectOption(label="30 minutes before", value="30",     emoji="⏰"),
    discord.SelectOption(label="45 minutes before", value="45",     emoji="⏰"),
    discord.SelectOption(label="1 hour before",     value="60",     emoji="⏰"),
    discord.SelectOption(label="2 hours before",    value="120",    emoji="⏰"),
    discord.SelectOption(label="Custom",            value="custom", emoji="⚙️",
                         description="Enter a custom number of minutes in the form"),
]

# ── Recurrence options for the Step 1 select menu ────────────────────────────
RECUR_OPTIONS = [
    discord.SelectOption(label="Single Event", value="none",      emoji="📅",
                         description="One-time event, no repeat"),
    discord.SelectOption(label="Daily",        value="daily",     emoji="🔁",
                         description="Repeats every day"),
    discord.SelectOption(label="Weekly",       value="weekly",    emoji="🔁",
                         description="Repeats every week"),
    discord.SelectOption(label="Bi-Weekly",    value="biweekly",  emoji="🔁",
                         description="Repeats every 2 weeks"),
    discord.SelectOption(label="Bi-Monthly",   value="bimonthly", emoji="🔁",
                         description="Repeats every 2 months"),
    discord.SelectOption(label="Monthly",      value="monthly",   emoji="🔁",
                         description="Repeats every month"),
    discord.SelectOption(label="Custom",       value="custom",    emoji="⚙️",
                         description="Set a custom number of days between events"),
]

# Human-readable labels used in confirmation messages and embeds
RECUR_LABELS = {
    "none":      "One-time",
    "daily":     "Daily",
    "weekly":    "Weekly",
    "biweekly":  "Bi-Weekly",
    "bimonthly": "Bi-Monthly",
    "monthly":   "Monthly",
    "custom":    "Custom",
}


def get_guild_event_count(guild_id: int) -> int:
    """Return how many events currently exist for a guild."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM events WHERE guild_id = ?", (guild_id,)
        ).fetchone()
    return row["cnt"] if row else 0


def compute_next_start(start_iso: str, recur_rule: str, recur_interval: int) -> str | None:
    """
    Given an ISO start datetime and recurrence rule, return the ISO string
    for the next occurrence. Returns None for single/no-recur events.
    """
    if not recur_rule or recur_rule == "none":
        return None

    dt = datetime.fromisoformat(start_iso)

    if recur_rule == "daily":
        next_dt = dt + timedelta(days=1)
    elif recur_rule == "weekly":
        next_dt = dt + timedelta(weeks=1)
    elif recur_rule == "biweekly":
        next_dt = dt + timedelta(weeks=2)
    elif recur_rule in ("bimonthly", "monthly") and HAS_DATEUTIL:
        months  = 2 if recur_rule == "bimonthly" else 1
        next_dt = dt + relativedelta(months=months)
    elif recur_rule in ("bimonthly", "monthly"):
        days    = 61 if recur_rule == "bimonthly" else 30
        next_dt = dt + timedelta(days=days)
    elif recur_rule == "custom":
        next_dt = dt + timedelta(days=max(recur_interval, 1))
    else:
        return None

    return next_dt.isoformat()


async def post_event_embed(channel: discord.TextChannel, event_data: dict):
    """
    Build and post the event embed with the full button row (RSVP + Edit).
    Fetches the guild embed color and saves the message_id back to the database.
    """
    from cogs.rsvp import EventView   # Local import to avoid circular dependency

    # Attach guild embed color to the event dict for the embed builder
    cfg = get_guild_config(channel.guild.id)
    event_data = {**event_data, "embed_color": cfg.get("embed_color") if cfg else None}

    rsvps = {"accepted": [], "declined": [], "tentative": []}
    embed = build_event_embed(event_data, rsvps)
    view  = EventView(event_id=event_data["id"], event=event_data)
    msg   = await channel.send(embed=embed, view=view)

    with get_connection() as conn:
        conn.execute(
            "UPDATE events SET message_id = ? WHERE id = ?",
            (msg.id, event_data["id"])
        )
        conn.commit()


# ── Step 2: Event detail modal ────────────────────────────────────────────────
class NewEventModal(discord.ui.Modal):
    """
    The main event creation form. Timezone has already been chosen via
    the TimezoneSelectView before this modal opens, so it's passed in
    as a parameter. The last field is either repeat interval (custom
    recurrence) or the Tentative button toggle (all other types).
    Button label customization is available after creation via /eventbuttons.
    """

    def __init__(self, channel: discord.TextChannel, recur_rule: str,
                 tz_name: str, reminder_offset: int,
                 notify_role_id: int | None = None, *args, **kwargs):
        super().__init__(title="Create a New Event", *args, **kwargs)
        self.target_channel  = channel
        self.recur_rule      = recur_rule
        self.tz_name         = tz_name
        self.reminder_offset = reminder_offset  # -1 means custom (user enters in form)
        self.notify_role_id  = notify_role_id

        self.add_item(discord.ui.InputText(
            label="Event Title",
            placeholder="e.g. Weekly Raid Night",
            max_length=100,
        ))
        self.add_item(discord.ui.InputText(
            label="Description (optional)",
            placeholder="What's happening? Any details...",
            style=discord.InputTextStyle.paragraph,
            required=False,
            max_length=500,
        ))
        self.add_item(discord.ui.InputText(
            label="Start Date & Time",
            placeholder="2026-07-04 20:00  or  July 4 8pm  or  next Friday 9pm",
            max_length=50,
        ))
        self.add_item(discord.ui.InputText(
            label="End Date & Time (optional)",
            placeholder="2026-07-04 21:00  or  July 4 9pm",
            required=False,
            max_length=50,
        ))
        if recur_rule == "custom":
            self.add_item(discord.ui.InputText(
                label="Repeat Interval (days)",
                placeholder="e.g. 14 for every 2 weeks",
                max_length=4,
            ))
        elif reminder_offset == -1:
            self.add_item(discord.ui.InputText(
                label="Custom Reminder (minutes before start)",
                placeholder="e.g. 90 for 1.5 hours before",
                max_length=4,
            ))
        else:
            self.add_item(discord.ui.InputText(
                label="Notify Role (optional)",
                placeholder="Role name e.g. Wise Guy, or role ID, or leave blank",
                required=False,
                max_length=100,
            ))

    async def callback(self, interaction: discord.Interaction):
        # Defer immediately — dateparser can be slow and we need > 3 seconds
        await interaction.response.defer(ephemeral=True)

        guild_id = interaction.guild.id

        # Free tier cap
        if not is_premium(guild_id) and get_guild_event_count(guild_id) >= FREE_EVENT_LIMIT:
            await interaction.followup.send(
                embed=build_error_embed(
                    f"Free servers are limited to **{FREE_EVENT_LIMIT} events**. "
                    "Delete an old event or upgrade to Premium for unlimited events."
                ),
                ephemeral=True,
            )
            return

        title       = self.children[0].value.strip()
        description = self.children[1].value.strip() if self.children[1].value else ""
        start_raw   = self.children[2].value.strip()
        end_raw     = self.children[3].value.strip() if self.children[3].value else None

        recur_interval = 7
        reminder_offset = self.reminder_offset

        if self.recur_rule == "custom" and len(self.children) > 4:
            try:
                recur_interval = max(1, int(self.children[4].value.strip()))
            except ValueError:
                recur_interval = 7
        elif self.reminder_offset == -1 and len(self.children) > 4:
            # Custom reminder — read from the 5th field
            try:
                reminder_offset = max(1, int(self.children[4].value.strip()))
            except ValueError:
                reminder_offset = 15

        # Parse start datetime
        start_dt = _parse_datetime(start_raw, self.tz_name)
        if not start_dt:
            await interaction.followup.send(
                embed=build_error_embed(
                    "Couldn't parse the start date/time. Try `2026-07-04 20:00` "
                    "or something like `July 4 8pm`."
                ),
                ephemeral=True,
            )
            return
        start_iso = start_dt.isoformat()

        # Parse optional end datetime
        end_iso = None
        if end_raw:
            end_dt = _parse_datetime(end_raw, self.tz_name)
            if end_dt:
                end_iso = end_dt.isoformat()

        is_recurring = 0 if self.recur_rule == "none" else 1

        # Resolve notify role BEFORE the INSERT so it gets saved correctly
        notify_role_id = self.notify_role_id
        if self.recur_rule != "custom" and self.reminder_offset != -1 and len(self.children) > 4:
            role_raw = self.children[4].value.strip() if self.children[4].value else ""
            if role_raw:
                # Try as role ID first
                try:
                    role = interaction.guild.get_role(int(role_raw))
                    if role:
                        notify_role_id = role.id
                except ValueError:
                    # Try as role name (case-insensitive)
                    clean = role_raw.lstrip("@").strip()
                    role = discord.utils.find(
                        lambda r: r.name.lower() == clean.lower(),
                        interaction.guild.roles
                    )
                    if role:
                        notify_role_id = role.id
                    else:
                        log.warning(
                            f"Could not find role '{role_raw}' in guild {interaction.guild.id}"
                        )

        with get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO events
                    (guild_id, channel_id, creator_id, title, description,
                     timezone, start_time, end_time, is_recurring,
                     recur_rule, recur_interval, reminder_offset, notify_role_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id, self.target_channel.id, interaction.user.id,
                    title, description, self.tz_name, start_iso, end_iso,
                    is_recurring, self.recur_rule, recur_interval, reminder_offset,
                    notify_role_id,
                ),
            )
            event_id = cursor.lastrowid
            conn.commit()

        show_tentative = 1  # Default: show tentative. Use /eventbuttons to toggle.

        event = {
            "id": event_id, "title": title, "description": description,
            "timezone": self.tz_name, "start_time": start_iso, "end_time": end_iso,
            "is_recurring": is_recurring, "recur_rule": self.recur_rule,
            "recur_interval": recur_interval, "channel_id": self.target_channel.id,
            "reminder_offset": reminder_offset,
            "notify_role_id": self.notify_role_id,
            "btn_accept_label":      "✅ Accept",
            "btn_decline_label":     "❌ Decline",
            "btn_tentative_label":   "❓ Tentative",
            "btn_tentative_enabled": show_tentative,
        }

        await post_event_embed(self.target_channel, event)

        recur_str = RECUR_LABELS.get(self.recur_rule, self.recur_rule)
        if self.recur_rule == "custom":
            recur_str = f"Every {recur_interval} days"

        log.info(
            f"Event created: '{title}' (ID {event_id}) in guild "
            f"{interaction.guild.id} by {interaction.user} "
            f"— recur={self.recur_rule}, tz={self.tz_name}, tentative={bool(show_tentative)}"
        )

        await interaction.followup.send(
            embed=build_success_embed(
                f"**{title}** created in {self.target_channel.mention}! "
                f"(ID: `{event_id}` · {recur_str})\n"
                "Use `/eventbuttons` to toggle the Tentative button anytime."
            ),
            ephemeral=True,
        )


# ── Reminder selector (Step 3 of 4) ─────────────────────────────────────────
class ReminderSelectView(discord.ui.View):
    """
    Shown after the user picks a timezone.
    Lets them choose when to send the reminder before opening the detail modal.
    """

    def __init__(self, channel: discord.TextChannel, author_id: int,
                 recur_rule: str, tz_name: str):
        super().__init__(timeout=60)
        self.channel    = channel
        self.author_id  = author_id
        self.recur_rule = recur_rule
        self.tz_name    = tz_name

    @discord.ui.select(placeholder="Choose reminder timing...", options=REMINDER_OPTIONS)
    async def reminder_select(self, select: discord.ui.Select,
                              interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran `/newevent` can use this menu.",
                ephemeral=True,
            )
            return

        self.stop()
        value = select.values[0]
        reminder_offset = -1 if value == "custom" else int(value)

        await interaction.response.send_modal(
            NewEventModal(
                channel=self.channel,
                recur_rule=self.recur_rule,
                tz_name=self.tz_name,
                reminder_offset=reminder_offset,
                notify_role_id=None,
            )
        )


# ── Timezone selector (sits between recurrence pick and detail modal) ─────────
class TimezoneSelectView(discord.ui.View):
    """
    Shown after the user picks a recurrence type.
    Lets them choose a timezone from a dropdown before the detail modal opens.
    """

    def __init__(self, channel: discord.TextChannel, author_id: int, recur_rule: str):
        super().__init__(timeout=60)
        self.channel    = channel
        self.author_id  = author_id
        self.recur_rule = recur_rule

    @discord.ui.select(placeholder="Choose your timezone...", options=NA_TIMEZONES)
    async def tz_select(self, select: discord.ui.Select, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran `/newevent` can use this menu.",
                ephemeral=True,
            )
            return

        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="📅  New Event — Step 3 of 4",
                description="**When should the reminder be sent?**",
                color=COLOR_EVENT,
            ),
            view=ReminderSelectView(
                channel=self.channel,
                author_id=self.author_id,
                recur_rule=self.recur_rule,
                tz_name=select.values[0],
            ),
        )


# ── Step 1: Recurrence type selector ─────────────────────────────────────────
class RecurrenceSelectView(discord.ui.View):
    """
    Ephemeral select menu sent to the event creator after /newevent.
    Choosing a recurrence type opens the TimezoneSelectView.
    Times out after 60 seconds if ignored.
    """

    def __init__(self, channel: discord.TextChannel, author_id: int):
        super().__init__(timeout=60)
        self.channel   = channel
        self.author_id = author_id

    @discord.ui.select(placeholder="Choose event type...", options=RECUR_OPTIONS)
    async def recur_select(self, select: discord.ui.Select, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran `/newevent` can use this menu.",
                ephemeral=True,
            )
            return

        self.stop()
        # Edit the message to show the timezone picker (can't open modal from select directly
        # when we want to swap the view — so we update the embed and swap the view)
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="📅  New Event — Step 2 of 4",
                description="**Choose your timezone.**",
                color=COLOR_EVENT,
            ),
            view=TimezoneSelectView(
                channel=self.channel,
                author_id=self.author_id,
                recur_rule=select.values[0],
            ),
        )


# ── Edit Event Modal ──────────────────────────────────────────────────────────
class EditEventModal(discord.ui.Modal):
    """
    Pre-filled edit form. Triggered by:
      - The ✏️ Edit button on the event embed (see cogs/rsvp.py → EventView)
      - The /editevent slash command (fallback)

    After saving, the live event embed is refreshed automatically.
    The timezone field remains a free-text input here since we're editing
    an existing event that already has a stored timezone.
    """

    def __init__(self, event: dict, *args, **kwargs):
        super().__init__(title=f"Edit: {event['title'][:40]}", *args, **kwargs)
        self.event = event

        self.add_item(discord.ui.InputText(
            label="Event Title",
            value=event["title"],
            max_length=100,
        ))
        self.add_item(discord.ui.InputText(
            label="Description",
            value=event.get("description") or "",
            style=discord.InputTextStyle.paragraph,
            required=False,
            max_length=500,
        ))
        self.add_item(discord.ui.InputText(
            label="Start Date & Time",
            value=event["start_time"][:16].replace("T", " "),
            placeholder="2026-07-04 20:00  or  July 4 8pm",
            max_length=50,
        ))
        self.add_item(discord.ui.InputText(
            label="Timezone",
            value=event.get("timezone") or "UTC",
            max_length=50,
        ))
        self.add_item(discord.ui.InputText(
            label="Reminder (minutes before start)",
            value=str(event.get("reminder_offset") or 15),
            max_length=4,
        ))

    async def callback(self, interaction: discord.Interaction):
        from cogs.rsvp import refresh_event_embed

        title        = self.children[0].value.strip()
        description  = self.children[1].value.strip()
        start_raw    = self.children[2].value.strip()
        tz_name      = self.children[3].value.strip() or "UTC"
        reminder_raw = self.children[4].value.strip()

        try:
            reminder = int(reminder_raw)
        except ValueError:
            reminder = 15

        if tz_name not in pytz.all_timezones:
            await interaction.response.send_message(
                embed=build_error_embed(f"Unknown timezone: `{tz_name}`."),
                ephemeral=True,
            )
            return

        start_dt = _parse_datetime(start_raw, tz_name)
        if not start_dt:
            await interaction.response.send_message(
                embed=build_error_embed(
                    "Couldn't parse the date/time. Try `2026-07-04 20:00` "
                    "or something like `July 4 8pm`."
                ),
                ephemeral=True,
            )
            return
        start_iso = start_dt.isoformat()

        with get_connection() as conn:
            conn.execute(
                """
                UPDATE events
                SET title=?, description=?, start_time=?, timezone=?, reminder_offset=?
                WHERE id=?
                """,
                (title, description, start_iso, tz_name, reminder, self.event["id"]),
            )
            conn.commit()

        await interaction.response.send_message(
            embed=build_success_embed(f"**{title}** has been updated."),
            ephemeral=True,
        )

        await refresh_event_embed(self.event["id"], interaction.guild, interaction.client)


# ── /eventbuttons modal ──────────────────────────────────────────────────────
class EventButtonsModal(discord.ui.Modal):
    """
    Opened by /eventbuttons. Lets the creator toggle the Tentative button
    on an existing event. The embed refreshes immediately after saving.
    Label customization is reserved for a future premium feature.
    """

    def __init__(self, event: dict, *args, **kwargs):
        super().__init__(title=f"Button Settings: {event['title'][:35]}", *args, **kwargs)
        self.event = event

        current = "yes" if event.get("btn_tentative_enabled", 1) else "no"
        self.add_item(discord.ui.InputText(
            label="Show Tentative Button? (yes / no)",
            value=current,
            max_length=3,
        ))

    async def callback(self, interaction: discord.Interaction):
        from cogs.rsvp import refresh_event_embed

        show_raw       = self.children[0].value.strip().lower()
        show_tentative = 0 if show_raw in ("no", "n", "false", "0") else 1

        with get_connection() as conn:
            conn.execute(
                "UPDATE events SET btn_tentative_enabled=? WHERE id=?",
                (show_tentative, self.event["id"]),
            )
            conn.commit()

        log.info(
            f"Button config updated for event {self.event['id']} "
            f"by {interaction.user} — show_tentative={bool(show_tentative)}"
        )

        state = "visible" if show_tentative else "hidden"
        await interaction.response.send_message(
            embed=build_success_embed(
                f"Updated **{self.event['title']}** — "
                f"Tentative button is now **{state}**."
            ),
            ephemeral=True,
        )

        await refresh_event_embed(self.event["id"], interaction.guild, interaction.client)

# ── Cog ───────────────────────────────────────────────────────────────────────
class Events(commands.Cog):
    """Core event management commands."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="newevent", description="Create a new event.")
    async def newevent(
        self,
        ctx: discord.ApplicationContext,
        channel: discord.Option(
            discord.TextChannel,
            description="Channel to post the event in.",
            required=True,
        ),
    ):
        """Step 1: recurrence type. Step 2: timezone. Step 3: reminder. Step 4: detail modal."""
        if not check_setup(ctx.guild.id):
            await ctx.respond(
                embed=build_error_embed("Run `/setup` first to configure Soren."),
                ephemeral=True,
            )
            return

        if not is_event_creator(ctx.author):
            await ctx.respond(
                embed=build_error_embed("You don't have the Event Creator role."),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📅  New Event — Step 1 of 4",
            description=(
                f"Posting to: {channel.mention}\n\n"
                "**Select the event type below.**\n"
                "A form will appear for you to fill in the details."
            ),
            color=COLOR_EVENT,
        )
        await ctx.respond(
            embed=embed,
            view=RecurrenceSelectView(channel=channel, author_id=ctx.author.id),
            ephemeral=True,
        )

    @discord.slash_command(name="editevent", description="Edit an existing event by its ID.")
    async def editevent(
        self,
        ctx: discord.ApplicationContext,
        event_id: discord.Option(int, description="The event ID to edit.", required=True),
    ):
        if not is_event_creator(ctx.author):
            await ctx.respond(
                embed=build_error_embed("You don't have the Event Creator role."),
                ephemeral=True,
            )
            return

        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM events WHERE id=? AND guild_id=?",
                (event_id, ctx.guild.id),
            ).fetchone()

        if not row:
            await ctx.respond(
                embed=build_error_embed(f"No event found with ID `{event_id}` in this server."),
                ephemeral=True,
            )
            return

        await ctx.send_modal(EditEventModal(event=dict(row)))

    @discord.slash_command(name="deleteevent", description="Delete an event permanently.")
    async def deleteevent(
        self,
        ctx: discord.ApplicationContext,
        event_id: discord.Option(int, description="The event ID to delete.", required=True),
    ):
        if not is_event_creator(ctx.author):
            await ctx.respond(
                embed=build_error_embed("You don't have the Event Creator role."),
                ephemeral=True,
            )
            return

        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM events WHERE id=? AND guild_id=?",
                (event_id, ctx.guild.id),
            ).fetchone()

        if not row:
            await ctx.respond(
                embed=build_error_embed(f"No event found with ID `{event_id}`."),
                ephemeral=True,
            )
            return

        event = dict(row)

        try:
            ch = ctx.guild.get_channel(event["channel_id"])
            if ch and event.get("message_id"):
                msg = await ch.fetch_message(event["message_id"])
                await msg.delete()
        except discord.NotFound:
            pass

        with get_connection() as conn:
            conn.execute("DELETE FROM events WHERE id=?", (event_id,))
            conn.commit()

        await ctx.respond(
            embed=build_success_embed(
                f"Event **{event['title']}** (ID: `{event_id}`) deleted."
            ),
            ephemeral=True,
        )

    @discord.slash_command(name="listevents", description="List all upcoming events in this server.")
    async def listevents(self, ctx: discord.ApplicationContext):
        now_iso = datetime.utcnow().isoformat()

        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, title, start_time, timezone, channel_id, is_recurring, recur_rule
                FROM events
                WHERE guild_id=? AND start_time >= ?
                ORDER BY start_time ASC LIMIT 20
                """,
                (ctx.guild.id, now_iso),
            ).fetchall()

        if not rows:
            await ctx.respond(
                embed=build_error_embed("No upcoming events. Create one with `/newevent`!"),
                ephemeral=True,
            )
            return

        embed = discord.Embed(title="📅  Upcoming Events", color=COLOR_EVENT)
        for row in rows:
            try:
                tz = pytz.timezone(row["timezone"] or "UTC")
                dt = datetime.fromisoformat(row["start_time"]).astimezone(tz)
                ts = dt.strftime("%b %d, %Y  %I:%M %p %Z")
            except Exception:
                ts = row["start_time"]

            recur_tag = f"  🔁 {RECUR_LABELS.get(row['recur_rule'], row['recur_rule'])}" \
                        if row["is_recurring"] else ""
            ch     = ctx.guild.get_channel(row["channel_id"])
            ch_str = ch.mention if ch else "unknown channel"

            embed.add_field(
                name=f"[ID: {row['id']}]  {row['title']}{recur_tag}",
                value=f"🕐 {ts}  •  {ch_str}",
                inline=False,
            )

        await ctx.respond(embed=embed, ephemeral=True)


    @discord.slash_command(name="eventbuttons", description="Customize the RSVP button labels for an event.")
    async def eventbuttons(
        self,
        ctx: discord.ApplicationContext,
        event_id: discord.Option(int, description="The event ID to customize.", required=True),
    ):
        """Opens a modal to set custom labels and toggle the Tentative button."""
        if not is_event_creator(ctx.author):
            await ctx.respond(
                embed=build_error_embed("You need the **Event Creator** role to customize buttons."),
                ephemeral=True,
            )
            return

        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM events WHERE id=? AND guild_id=?",
                (event_id, ctx.guild.id),
            ).fetchone()

        if not row:
            await ctx.respond(
                embed=build_error_embed(f"No event found with ID `{event_id}`."),
                ephemeral=True,
            )
            return

        await ctx.send_modal(EventButtonsModal(event=dict(row)))


def setup(bot: discord.Bot):
    bot.add_cog(Events(bot))