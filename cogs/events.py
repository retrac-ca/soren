"""
cogs/events.py
===============
Slash commands for creating, editing, and deleting events.

Commands
--------
/newevent          — 4-step event creation flow
/editeventdetails  — Edit title, description, max RSVPs, notify role
/editeventtime     — Edit start/end time, timezone, reminder offset
/deleteevent       — Delete an event (with confirmation)
/listevents        — List all upcoming events in this server
/eventbuttons      — Customize RSVP button labels and toggle tentative
/cancelevent       — Soft cancel an event
/myevents          — List events the caller has RSVPed to
/exportevents      — Export upcoming events as a .ics calendar file

Event creation flow
-------------------
Step 1 — What type of event?        (RecurrenceSelectView)
Step 2 — What timezone?             (TimezoneSelectView)
Step 3 — Mention/remind a role?     (RolePingSelectView)
  └─ Yes → single-field modal       (RoleInputModal) → Step 4
  └─ No  → skip reminder entirely   → Step 4
Step 4 — Fill in event details      (NewEventModal)
  Fields: Title · Description · Start · End · custom interval (conditional)
  Notify Role field removed — handled in Step 3.
  If no role was set, reminder_offset defaults to 15 min silently.
"""

import discord
from discord.ext import commands
from datetime import datetime, timedelta, timezone
import pytz
import io

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
    tz = pytz.timezone(tz_name)
    try:
        return tz.localize(datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M"))
    except ValueError:
        pass
    if HAS_DATEPARSER:
        settings = {
            "TIMEZONE": tz_name,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
            "DATE_ORDER": "MDY",
        }
        parsed = dateparser.parse(raw.strip(), settings=settings)
        if parsed:
            return parsed.astimezone(tz)
    return None


try:
    from dateutil.relativedelta import relativedelta
    HAS_DATEUTIL = True
except ImportError:
    HAS_DATEUTIL = False

# ── Tier event limits ─────────────────────────────────────────────────────────
FREE_EVENT_LIMIT    = 10
PREMIUM_EVENT_LIMIT = 50

# ── Default reminder offset ───────────────────────────────────────────────────
DEFAULT_REMINDER_OFFSET = 15  # minutes — used when no role is set

# ── Timezone options ──────────────────────────────────────────────────────────
TIMEZONES = [
    discord.SelectOption(label="Eastern Time (ET)",           value="America/New_York",    emoji="🕐", description="UTC-5 / UTC-4 (DST)"),
    discord.SelectOption(label="Central Time (CT)",           value="America/Chicago",     emoji="🕐", description="UTC-6 / UTC-5 (DST)"),
    discord.SelectOption(label="Mountain Time (MT)",          value="America/Denver",      emoji="🕐", description="UTC-7 / UTC-6 (DST)"),
    discord.SelectOption(label="Mountain Time - AZ (no DST)", value="America/Phoenix",     emoji="🕐", description="UTC-7, no daylight saving"),
    discord.SelectOption(label="Pacific Time (PT)",           value="America/Los_Angeles", emoji="🕐", description="UTC-8 / UTC-7 (DST)"),
    discord.SelectOption(label="Alaska Time (AKT)",           value="America/Anchorage",   emoji="🕐", description="UTC-9 / UTC-8 (DST)"),
    discord.SelectOption(label="Hawaii Time (HT)",            value="Pacific/Honolulu",    emoji="🕐", description="UTC-10, no daylight saving"),
    discord.SelectOption(label="Atlantic Time (AT)",          value="America/Halifax",     emoji="🕐", description="UTC-4 / UTC-3 (DST)"),
    discord.SelectOption(label="Newfoundland Time (NT)",      value="America/St_Johns",    emoji="🕐", description="UTC-3:30 / UTC-2:30 (DST)"),
    discord.SelectOption(label="London (GMT/BST)",            value="Europe/London",       emoji="🇬🇧", description="Europe/London"),
    discord.SelectOption(label="Berlin (CET/CEST)",           value="Europe/Berlin",       emoji="🇩🇪", description="Europe/Berlin"),
    discord.SelectOption(label="Sydney (AEDT/AEST)",          value="Australia/Sydney",    emoji="🇦🇺", description="Australia/Sydney"),
    discord.SelectOption(label="Tokyo (JST)",                 value="Asia/Tokyo",          emoji="🇯🇵", description="Asia/Tokyo"),
    discord.SelectOption(label="New Delhi (IST)",             value="Asia/Kolkata",        emoji="🇮🇳", description="Asia/Kolkata"),
    discord.SelectOption(label="UTC",                         value="UTC",                 emoji="🌐", description="Coordinated Universal Time"),
]

# ── Recurrence options ────────────────────────────────────────────────────────
RECUR_OPTIONS = [
    discord.SelectOption(label="Single Event", value="none",      emoji="📅", description="One-time event, no repeat"),
    discord.SelectOption(label="Daily",        value="daily",     emoji="🔁", description="Repeats every day"),
    discord.SelectOption(label="Weekly",       value="weekly",    emoji="🔁", description="Repeats every week"),
    discord.SelectOption(label="Bi-Weekly",    value="biweekly",  emoji="🔁", description="Repeats every 2 weeks"),
    discord.SelectOption(label="Bi-Monthly",   value="bimonthly", emoji="🔁", description="Repeats every 2 months"),
    discord.SelectOption(label="Monthly",      value="monthly",   emoji="🔁", description="Repeats every month"),
    discord.SelectOption(label="Custom",       value="custom",    emoji="⚙️", description="Set a custom number of days between events"),
]

RECUR_LABELS = {
    "none":      "One-time",
    "daily":     "Daily",
    "weekly":    "Weekly",
    "biweekly":  "Bi-Weekly",
    "bimonthly": "Bi-Monthly",
    "monthly":   "Monthly",
    "custom":    "Custom",
}

# ── Role ping yes/no options ──────────────────────────────────────────────────
ROLE_PING_OPTIONS = [
    discord.SelectOption(
        label="Yes — mention a role",
        value="yes",
        emoji="🔔",
        description="Ping a role when the event posts and at reminder time",
    ),
    discord.SelectOption(
        label="No — skip role mention",
        value="no",
        emoji="🔕",
        description="No role ping — go straight to event details",
    ),
]


# ── Autocomplete helpers ──────────────────────────────────────────────────────

async def autocomplete_event_ids(ctx: discord.AutocompleteContext):
    now = datetime.now(timezone.utc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, title FROM events WHERE guild_id=? AND start_time >= ? ORDER BY start_time ASC LIMIT 25",
            (ctx.interaction.guild.id, start_of_day)
        ).fetchall()
    return [discord.OptionChoice(name=f"{row['id']}: {row['title'][:80]}", value=row['id']) for row in rows]


# ── Misc helpers ──────────────────────────────────────────────────────────────

def build_listevents_embed(rows, guild):
    embed = discord.Embed(title="📅  Upcoming Events", color=COLOR_EVENT)
    for row in rows:
        try:
            tz = pytz.timezone(row["timezone"] or "UTC")
            dt = datetime.fromisoformat(row["start_time"]).astimezone(tz)
            ts = dt.strftime("%b %d, %Y  %I:%M %p %Z")
        except Exception:
            ts = row["start_time"]
        recur_tag = f"  🔁 {RECUR_LABELS.get(row['recur_rule'], row['recur_rule'])}" if row["is_recurring"] else ""
        ch = guild.get_channel(row["channel_id"])
        embed.add_field(
            name=f"[ID: {row['id']}]  {row['title']}{recur_tag}",
            value=f"🕐 {ts}  •  {ch.mention if ch else 'unknown channel'}",
            inline=False,
        )
    return embed


def get_guild_event_count(guild_id: int) -> int:
    """Count only upcoming (active) events. Past events don't count against the limit."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM events WHERE guild_id = ? AND start_time >= ?",
            (guild_id, now_iso),
        ).fetchone()
    return row["cnt"] if row else 0


def compute_next_start(start_iso: str, recur_rule: str, recur_interval: int) -> str | None:
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
        months = 2 if recur_rule == "bimonthly" else 1
        next_dt = dt + relativedelta(months=months)
    elif recur_rule in ("bimonthly", "monthly"):
        days = 61 if recur_rule == "bimonthly" else 30
        next_dt = dt + timedelta(days=days)
    elif recur_rule == "custom":
        next_dt = dt + timedelta(days=max(recur_interval, 1))
    else:
        return None
    return next_dt.isoformat()


async def post_event_embed(channel: discord.TextChannel, event_data: dict):
    """Build and post the event embed. Pings notify_role on creation if set."""
    from cogs.rsvp import EventView
    cfg = get_guild_config(channel.guild.id)
    event_data = {**event_data, "embed_color": cfg.get("embed_color") if cfg else None}
    rsvps = {"accepted": [], "declined": [], "tentative": []}
    embed = build_event_embed(event_data, rsvps)
    view  = EventView(event_id=event_data["id"], event=event_data)

    ping_content = None
    if event_data.get("notify_role_id"):
        role = channel.guild.get_role(event_data["notify_role_id"])
        if role:
            ping_content = role.mention

    msg = await channel.send(content=ping_content, embed=embed, view=view)
    with get_connection() as conn:
        conn.execute("UPDATE events SET message_id = ? WHERE id = ?", (msg.id, event_data["id"]))
        conn.commit()


async def repost_recurring_embed(bot: discord.Bot, event_id: int):
    """
    Deletes the old embed and posts a fresh one after a recurring event advances.
    No role ping on auto-respawn — only on manual creation.
    """
    from cogs.rsvp import EventView
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not row:
        return
    event = dict(row)

    guild = bot.get_guild(event["guild_id"])
    if not guild:
        return
    channel = guild.get_channel(event["channel_id"])
    if not channel:
        return

    if event.get("message_id"):
        try:
            old_msg = await channel.fetch_message(event["message_id"])
            await old_msg.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

    cfg = get_guild_config(guild.id)
    event = {**event, "embed_color": cfg.get("embed_color") if cfg else None}
    rsvps = {"accepted": [], "declined": [], "tentative": []}
    embed = build_event_embed(event, rsvps)
    view  = EventView(event_id=event_id, event=event)

    try:
        new_msg = await channel.send(embed=embed, view=view)
        with get_connection() as conn:
            conn.execute("UPDATE events SET message_id=? WHERE id=?", (new_msg.id, event_id))
            conn.commit()
        log.info(f"Reposted embed for recurring event {event_id}")
    except discord.Forbidden:
        log.warning(f"repost_recurring_embed: no permission in channel {channel.id}")


# ── Step 4 — Event detail modal ───────────────────────────────────────────────

class NewEventModal(discord.ui.Modal):
    """
    Final step of /newevent.
    reminder_offset is passed in from earlier steps.
    wants_role=True  → slot 5 = role name field (resolved in callback).
    wants_role=False → slot 5 = custom interval field (only if recur_rule=="custom").
    If both wants_role and custom recur are needed, role takes slot 5 and
    recur_interval falls back to 7 days (editable via /editeventtime).
    """

    def __init__(self, channel: discord.TextChannel, recur_rule: str,
                 tz_name: str, reminder_offset: int,
                 wants_role: bool = False, *args, **kwargs):
        super().__init__(title="Create a New Event", *args, **kwargs)
        self.target_channel  = channel
        self.recur_rule      = recur_rule
        self.tz_name         = tz_name
        self.reminder_offset = reminder_offset
        self.wants_role      = wants_role

        self.add_item(discord.ui.InputText(label="Event Title", placeholder="e.g. Weekly Raid Night", max_length=100))
        self.add_item(discord.ui.InputText(label="Description (optional)", placeholder="What's happening? Any details...", style=discord.InputTextStyle.paragraph, required=False, max_length=500))
        self.add_item(discord.ui.InputText(label="Start Date & Time", placeholder="2026-07-04 20:00  or  July 4 8pm  or  next Friday 9pm", max_length=50))
        self.add_item(discord.ui.InputText(label="End Date & Time (optional)", placeholder="2026-07-04 21:00  or  July 4 9pm", required=False, max_length=50))
        # Slot 5: role field takes priority; custom interval is fallback if no role wanted
        if wants_role:
            self.add_item(discord.ui.InputText(
                label="Mention & Reminder Role",
                placeholder="Role name, @Role, or role ID  (e.g. Members)",
                max_length=100,
            ))
        elif recur_rule == "custom":
            self.add_item(discord.ui.InputText(label="Repeat Interval (days)", placeholder="e.g. 14 for every 2 weeks", max_length=4))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id
        premium  = is_premium(guild_id)

        active_count = get_guild_event_count(guild_id)
        if premium and active_count >= PREMIUM_EVENT_LIMIT:
            await interaction.followup.send(
                embed=build_error_embed(
                    f"Premium servers are limited to **{PREMIUM_EVENT_LIMIT} active events**. "
                    f"You currently have **{active_count}** — delete or wait for some to pass."
                ),
                ephemeral=True,
            )
            return
        elif not premium and active_count >= FREE_EVENT_LIMIT:
            await interaction.followup.send(
                embed=build_error_embed(
                    f"Free servers are limited to **{FREE_EVENT_LIMIT} active events** "
                    f"({active_count}/{FREE_EVENT_LIMIT} used). "
                    f"Delete an old event or upgrade to ⭐ Premium for up to {PREMIUM_EVENT_LIMIT}."
                ),
                ephemeral=True,
            )
            return

        title       = self.children[0].value.strip()
        description = self.children[1].value.strip() if self.children[1].value else ""
        start_raw   = self.children[2].value.strip()
        end_raw     = self.children[3].value.strip() if self.children[3].value else None

        recur_interval  = 7
        reminder_offset = self.reminder_offset
        notify_role_id  = None

        # Slot 5: resolve role OR custom interval depending on what was shown
        if self.wants_role and len(self.children) > 4:
            role_raw = self.children[4].value.strip() if self.children[4].value else ""
            if role_raw:
                try:
                    role = interaction.guild.get_role(int(role_raw))
                    if role:
                        notify_role_id = role.id
                except ValueError:
                    clean = role_raw.lstrip("@").strip()
                    role  = discord.utils.find(
                        lambda r: r.name.lower() == clean.lower(),
                        interaction.guild.roles,
                    )
                    if role:
                        notify_role_id = role.id
                if not notify_role_id:
                    log.warning(
                        f"NewEventModal: could not resolve role '{role_raw}' "
                        f"in guild {interaction.guild.id} — creating event without role"
                    )
        elif not self.wants_role and self.recur_rule == "custom" and len(self.children) > 4:
            try:
                recur_interval = max(1, int(self.children[4].value.strip()))
            except ValueError:
                recur_interval = 7

        # If wants_role + custom recur at the same time, role took slot 5 so
        # recur_interval stays at default 7 — user can adjust via /editeventtime
        custom_interval_note = ""
        if self.wants_role and self.recur_rule == "custom":
            custom_interval_note = "\n📝 Custom repeat interval defaulted to **7 days** — use `/editeventtime` to adjust."

        start_dt = _parse_datetime(start_raw, self.tz_name)
        if not start_dt:
            await interaction.followup.send(
                embed=build_error_embed("Couldn't parse the start date/time. Try `2026-07-04 20:00` or `July 4 8pm`."),
                ephemeral=True,
            )
            return
        start_iso = start_dt.isoformat()

        end_iso = None
        if end_raw:
            end_dt = _parse_datetime(end_raw, self.tz_name)
            if end_dt:
                end_iso = end_dt.isoformat()

        is_recurring = 0 if self.recur_rule == "none" else 1

        tz_warning = ""
        if self.tz_name == "UTC":
            tz_warning = "\n⚠️ Event was created in **UTC**. Use `/editeventtime` to change the timezone if needed."

        with get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO events
                    (guild_id, channel_id, creator_id, title, description,
                     timezone, start_time, end_time, is_recurring,
                     recur_rule, recur_interval, reminder_offset, notify_role_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (guild_id, self.target_channel.id, interaction.user.id,
                 title, description, self.tz_name, start_iso, end_iso,
                 is_recurring, self.recur_rule, recur_interval,
                 reminder_offset, notify_role_id),
            )
            event_id = cursor.lastrowid
            conn.commit()

        event = {
            "id": event_id, "title": title, "description": description,
            "timezone": self.tz_name, "start_time": start_iso, "end_time": end_iso,
            "is_recurring": is_recurring, "recur_rule": self.recur_rule,
            "recur_interval": recur_interval, "channel_id": self.target_channel.id,
            "reminder_offset": reminder_offset, "notify_role_id": notify_role_id,
            "btn_accept_label": "✅ Accept", "btn_decline_label": "❌ Decline",
            "btn_tentative_label": "❓ Tentative", "btn_tentative_enabled": 1,
        }

        await post_event_embed(self.target_channel, event)

        recur_str = RECUR_LABELS.get(self.recur_rule, self.recur_rule)
        if self.recur_rule == "custom":
            recur_str = f"Every {recur_interval} days"

        log.info(f"Event created: '{title}' (ID {event_id}) in guild {guild_id} by {interaction.user}")

        await interaction.followup.send(
            embed=build_success_embed(
                f"**{title}** created in {self.target_channel.mention}! "
                f"(ID: `{event_id}` · {recur_str}){tz_warning}{custom_interval_note}"
            ),
            ephemeral=True,
        )


# ── Step 3 — Role ping selector ───────────────────────────────────────────────

class RolePingSelectView(discord.ui.View):
    """
    Step 3 of /newevent.
    Yes → NewEventModal with wants_role=True (role field appears as slot 5).
    No  → NewEventModal with wants_role=False, no role, reminder defaults to 15 min.
    Opening a second modal from a modal callback is blocked by Discord (error 50035),
    so the role field is folded directly into NewEventModal instead.
    """

    def __init__(self, channel: discord.TextChannel, author_id: int,
                 recur_rule: str, tz_name: str):
        super().__init__(timeout=60)
        self.channel    = channel
        self.author_id  = author_id
        self.recur_rule = recur_rule
        self.tz_name    = tz_name

    @discord.ui.select(placeholder="Mention / remind a role?", options=ROLE_PING_OPTIONS)
    async def role_ping_select(self, select: discord.ui.Select, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran `/newevent` can use this menu.", ephemeral=True
            )
            return

        self.stop()
        wants_role = select.values[0] == "yes"

        await interaction.response.send_modal(
            NewEventModal(
                channel=self.channel,
                recur_rule=self.recur_rule,
                tz_name=self.tz_name,
                reminder_offset=DEFAULT_REMINDER_OFFSET,
                wants_role=wants_role,
            )
        )


# ── Step 2 — Timezone selector ────────────────────────────────────────────────

class TimezoneSelectView(discord.ui.View):
    def __init__(self, channel, author_id, recur_rule):
        super().__init__(timeout=60)
        self.channel    = channel
        self.author_id  = author_id
        self.recur_rule = recur_rule

    @discord.ui.select(placeholder="Choose your timezone...", options=TIMEZONES)
    async def tz_select(self, select, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran `/newevent` can use this menu.", ephemeral=True
            )
            return
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="📅  New Event — Step 3 of 4",
                description=(
                    "**Do you want to mention and remind a role?**\n\n"
                    "🔔 **Yes** — A role will be pinged when the event posts "
                    "and again when the reminder fires.\n"
                    "🔕 **No** — No role ping. Skip straight to event details."
                ),
                color=COLOR_EVENT,
            ),
            view=RolePingSelectView(
                channel=self.channel,
                author_id=self.author_id,
                recur_rule=self.recur_rule,
                tz_name=select.values[0],
            ),
        )


# ── Step 1 — Recurrence selector ─────────────────────────────────────────────

class RecurrenceSelectView(discord.ui.View):
    def __init__(self, channel, author_id):
        super().__init__(timeout=60)
        self.channel   = channel
        self.author_id = author_id

    @discord.ui.select(placeholder="Choose event type...", options=RECUR_OPTIONS)
    async def recur_select(self, select, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran `/newevent` can use this menu.", ephemeral=True
            )
            return
        self.stop()
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


# ── Edit Event Details Modal ──────────────────────────────────────────────────

class EditEventDetailsModal(discord.ui.Modal):
    """
    Edit title, description, max RSVPs, and notify role.
    Opened by /editeventdetails.
    """

    def __init__(self, event: dict, guild: discord.Guild, *args, **kwargs):
        super().__init__(title="Edit Event Details", *args, **kwargs)
        self.event = event

        notify_role_value = ""
        if event.get("notify_role_id"):
            role = guild.get_role(event["notify_role_id"])
            if role:
                notify_role_value = role.name

        self.add_item(discord.ui.InputText(label="Event Title", value=event["title"], max_length=100))
        self.add_item(discord.ui.InputText(label="Description (optional)", value=event.get("description") or "", style=discord.InputTextStyle.paragraph, required=False, max_length=500))
        self.add_item(discord.ui.InputText(label="Max RSVPs (0 = unlimited)", value=str(event.get("max_rsvp") or 0), max_length=6))
        self.add_item(discord.ui.InputText(
            label="Mention & Reminder Role (optional)",
            value=notify_role_value,
            placeholder="Role name, @Role, or role ID — leave blank to clear",
            required=False,
            max_length=100,
        ))

    async def callback(self, interaction: discord.Interaction):
        from cogs.rsvp import refresh_event_embed
        await interaction.response.defer(ephemeral=True)

        title        = self.children[0].value.strip()
        description  = self.children[1].value.strip()
        max_rsvp_raw = self.children[2].value.strip()
        role_raw     = self.children[3].value.strip()

        try:
            max_rsvp = max(0, int(max_rsvp_raw))
        except ValueError:
            max_rsvp = 0

        if role_raw:
            notify_role_id = None
            try:
                role = interaction.guild.get_role(int(role_raw))
                if role:
                    notify_role_id = role.id
            except ValueError:
                clean = role_raw.lstrip("@").strip()
                role  = discord.utils.find(lambda r: r.name.lower() == clean.lower(), interaction.guild.roles)
                if role:
                    notify_role_id = role.id
        else:
            notify_role_id = None  # blank = clear the role

        with get_connection() as conn:
            conn.execute(
                "UPDATE events SET title=?, description=?, max_rsvp=?, notify_role_id=? WHERE id=?",
                (title, description, max_rsvp, notify_role_id, self.event["id"]),
            )
            conn.commit()

        await interaction.followup.send(embed=build_success_embed(f"Details updated for **{title}**."), ephemeral=True)
        await refresh_event_embed(self.event["id"], interaction.guild, interaction.client)


# ── Edit Event Time Modal ─────────────────────────────────────────────────────

class EditEventTimeModal(discord.ui.Modal):
    """Edit start time, end time, timezone, and reminder offset."""

    def __init__(self, event: dict, *args, **kwargs):
        super().__init__(title="Edit Event Time", *args, **kwargs)
        self.event = event

        self.add_item(discord.ui.InputText(label="Start Date & Time", value=event["start_time"][:16].replace("T", " "), placeholder="2026-07-04 20:00  or  July 4 8pm", max_length=50))
        self.add_item(discord.ui.InputText(label="End Date & Time (optional)", value=event.get("end_time", "")[:16].replace("T", " ") if event.get("end_time") else "", placeholder="Leave blank for no end time", required=False, max_length=50))
        self.add_item(discord.ui.InputText(label="Timezone", value=event.get("timezone") or "UTC", placeholder="e.g. America/New_York, UTC, Europe/London", max_length=50))
        self.add_item(discord.ui.InputText(label="Reminder (minutes before start)", value=str(event.get("reminder_offset") or 15), max_length=4))

    async def callback(self, interaction: discord.Interaction):
        from cogs.rsvp import refresh_event_embed
        await interaction.response.defer(ephemeral=True)

        start_raw    = self.children[0].value.strip()
        end_raw      = self.children[1].value.strip()
        tz_name      = self.children[2].value.strip() or "UTC"
        reminder_raw = self.children[3].value.strip()

        if tz_name not in pytz.all_timezones:
            await interaction.followup.send(
                embed=build_error_embed(f"Unknown timezone: `{tz_name}`.\nUse a standard tz name like `America/New_York`, `Europe/London`, or `UTC`."),
                ephemeral=True,
            )
            return

        try:
            reminder = max(1, int(reminder_raw))
        except ValueError:
            reminder = 15

        start_dt = _parse_datetime(start_raw, tz_name)
        if not start_dt:
            await interaction.followup.send(embed=build_error_embed("Couldn't parse the start date/time. Try `2026-07-04 20:00` or `July 4 8pm`."), ephemeral=True)
            return
        start_iso = start_dt.isoformat()

        end_iso = None
        if end_raw:
            end_dt = _parse_datetime(end_raw, tz_name)
            if not end_dt:
                await interaction.followup.send(embed=build_error_embed("Couldn't parse the end date/time."), ephemeral=True)
                return
            end_iso = end_dt.isoformat()

        with get_connection() as conn:
            conn.execute(
                "UPDATE events SET start_time=?, end_time=?, timezone=?, reminder_offset=?, reminded_at=NULL WHERE id=?",
                (start_iso, end_iso, tz_name, reminder, self.event["id"]),
            )
            conn.commit()

        await interaction.followup.send(embed=build_success_embed(f"Time updated for **{self.event['title']}**."), ephemeral=True)
        await refresh_event_embed(self.event["id"], interaction.guild, interaction.client)


# ── Event Buttons Modal ───────────────────────────────────────────────────────

class EventButtonsModal(discord.ui.Modal):
    """Free: toggle Tentative only. Premium: toggle + custom labels."""

    def __init__(self, event: dict, premium: bool, *args, **kwargs):
        super().__init__(title="Button Settings", *args, **kwargs)
        self.event   = event
        self.premium = premium

        current = "yes" if event.get("btn_tentative_enabled", 1) else "no"
        self.add_item(discord.ui.InputText(label="Show Tentative Button? (yes / no)", value=current, max_length=3))

        if premium:
            self.add_item(discord.ui.InputText(label="Accept Button Label",   value=event.get("btn_accept_label")    or "✅ Accept",    max_length=20))
            self.add_item(discord.ui.InputText(label="Tentative Button Label", value=event.get("btn_tentative_label") or "❓ Tentative", max_length=20))
            self.add_item(discord.ui.InputText(label="Decline Button Label",   value=event.get("btn_decline_label")   or "❌ Decline",   max_length=20))
        else:
            self.add_item(discord.ui.InputText(label="Custom Labels (⭐ Premium only)", value="Upgrade to Premium to set custom button labels.", required=False, max_length=50))

    async def callback(self, interaction: discord.Interaction):
        from cogs.rsvp import refresh_event_embed

        show_raw       = self.children[0].value.strip().lower()
        show_tentative = 0 if show_raw in ("no", "n", "false", "0") else 1

        if self.premium:
            accept_label    = self.children[1].value.strip() or "✅ Accept"
            tentative_label = self.children[2].value.strip() or "❓ Tentative"
            decline_label   = self.children[3].value.strip() or "❌ Decline"
        else:
            accept_label    = self.event.get("btn_accept_label")    or "✅ Accept"
            tentative_label = self.event.get("btn_tentative_label") or "❓ Tentative"
            decline_label   = self.event.get("btn_decline_label")   or "❌ Decline"

        with get_connection() as conn:
            conn.execute(
                "UPDATE events SET btn_tentative_enabled=?, btn_accept_label=?, btn_tentative_label=?, btn_decline_label=? WHERE id=?",
                (show_tentative, accept_label, tentative_label, decline_label, self.event["id"]),
            )
            conn.commit()

        state      = "visible" if show_tentative else "hidden"
        label_note = " Labels updated." if self.premium else ""
        await interaction.response.send_message(
            embed=build_success_embed(f"Updated **{self.event['title']}** — Tentative button is now **{state}**.{label_note}"),
            ephemeral=True,
        )
        await refresh_event_embed(self.event["id"], interaction.guild, interaction.client)


# ── Delete confirmation view ──────────────────────────────────────────────────

class DeleteConfirmView(discord.ui.View):
    def __init__(self, event: dict, author_id: int):
        super().__init__(timeout=30)
        self.event     = event
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the person who ran this command can confirm.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, delete it", style=discord.ButtonStyle.danger)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.stop()
        try:
            ch = interaction.guild.get_channel(self.event["channel_id"])
            if ch and self.event.get("message_id"):
                msg = await ch.fetch_message(self.event["message_id"])
                await msg.delete()
        except discord.NotFound:
            pass

        with get_connection() as conn:
            conn.execute("DELETE FROM events WHERE id=?", (self.event["id"],))
            conn.commit()

        await interaction.response.edit_message(
            embed=build_success_embed(f"Event **{self.event['title']}** (ID: `{self.event['id']}`) has been deleted."),
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(description="❎  Deletion cancelled.", color=discord.Color.blurple()),
            view=None,
        )


# ── Waitlist RSVP view ────────────────────────────────────────────────────────

class WaitlistView(discord.ui.View):
    def __init__(self, event_id: int):
        super().__init__(timeout=60)
        self.event_id = event_id

    @discord.ui.button(label="📋 Join Waitlist", style=discord.ButtonStyle.secondary)
    async def join_waitlist(self, button: discord.ui.Button, interaction: discord.Interaction):
        with get_connection() as conn:
            existing = conn.execute(
                "SELECT id FROM waitlist WHERE event_id=? AND user_id=?",
                (self.event_id, interaction.user.id),
            ).fetchone()
            if existing:
                await interaction.response.send_message("You're already on the waitlist for this event.", ephemeral=True)
                return
            conn.execute("INSERT INTO waitlist (event_id, user_id) VALUES (?, ?)", (self.event_id, interaction.user.id))
            conn.commit()

        with get_connection() as conn:
            pos = conn.execute(
                "SELECT COUNT(*) as cnt FROM waitlist WHERE event_id=? AND id <= "
                "(SELECT id FROM waitlist WHERE event_id=? AND user_id=?)",
                (self.event_id, self.event_id, interaction.user.id),
            ).fetchone()["cnt"]

        await interaction.response.send_message(f"✅ You've been added to the waitlist! You are **#{pos}** in line.", ephemeral=True)


# ── List Events paginator ─────────────────────────────────────────────────────

class ListEventsView(discord.ui.View):
    def __init__(self, rows, guild, author_id):
        super().__init__(timeout=300)
        self.rows      = rows
        self.guild     = guild
        self.author_id = author_id
        self.page      = 0
        self.page_size = 10
        self._update_buttons()

    def _update_buttons(self):
        self.prev_button.disabled = self.page == 0
        self.next_button.disabled = (self.page + 1) * self.page_size >= len(self.rows)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This is not your list.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀️ Previous", style=discord.ButtonStyle.primary)
    async def prev_button(self, button, interaction):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="Next ▶️", style=discord.ButtonStyle.primary)
    async def next_button(self, button, interaction):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    def _build_embed(self):
        embed = discord.Embed(title="📅  Upcoming Events", color=COLOR_EVENT)
        start = self.page * self.page_size
        for row in self.rows[start:start + self.page_size]:
            try:
                tz = pytz.timezone(row["timezone"] or "UTC")
                dt = datetime.fromisoformat(row["start_time"]).astimezone(tz)
                ts = dt.strftime("%b %d, %Y  %I:%M %p %Z")
            except Exception:
                ts = row["start_time"]
            recur_tag = f"  🔁 {RECUR_LABELS.get(row['recur_rule'], row['recur_rule'])}" if row["is_recurring"] else ""
            ch = self.guild.get_channel(row["channel_id"])
            embed.add_field(
                name=f"[ID: {row['id']}]  {row['title']}{recur_tag}",
                value=f"🕐 {ts}  •  {ch.mention if ch else 'unknown channel'}",
                inline=False,
            )
        total = (len(self.rows) - 1) // self.page_size + 1
        embed.set_footer(text=f"Page {self.page + 1} of {total}")
        return embed


# ── Cog ───────────────────────────────────────────────────────────────────────

class Events(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="newevent", description="Create a new event.")
    async def newevent(
        self,
        ctx: discord.ApplicationContext,
        channel: discord.Option(discord.TextChannel, description="Channel to post the event in.", required=True),
    ):
        if not check_setup(ctx.guild.id):
            await ctx.respond(embed=build_error_embed("Run `/setup` first to configure Soren."), ephemeral=True)
            return
        if not is_event_creator(ctx.author):
            await ctx.respond(embed=build_error_embed("You don't have the Event Creator role."), ephemeral=True)
            return

        embed = discord.Embed(
            title="📅  New Event — Step 1 of 4",
            description=f"Posting to: {channel.mention}\n\n**Select the event type below.**",
            color=COLOR_EVENT,
        )
        await ctx.respond(embed=embed, view=RecurrenceSelectView(channel=channel, author_id=ctx.author.id), ephemeral=True)

    @discord.slash_command(name="editeventdetails", description="Edit an event's title, description, max RSVPs, or notify role.")
    async def editeventdetails(
        self,
        ctx: discord.ApplicationContext,
        event_id: discord.Option(int, description="The event ID to edit.", autocomplete=autocomplete_event_ids, required=True),
    ):
        if not is_event_creator(ctx.author):
            await ctx.respond(embed=build_error_embed("You don't have the Event Creator role."), ephemeral=True)
            return
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM events WHERE id=? AND guild_id=?", (event_id, ctx.guild.id)).fetchone()
        if not row:
            await ctx.respond(embed=build_error_embed(f"No event found with ID `{event_id}`."), ephemeral=True)
            return
        await ctx.send_modal(EditEventDetailsModal(event=dict(row), guild=ctx.guild))

    @discord.slash_command(name="editeventtime", description="Edit an event's start/end time, timezone, or reminder.")
    async def editeventtime(
        self,
        ctx: discord.ApplicationContext,
        event_id: discord.Option(int, description="The event ID to edit.", autocomplete=autocomplete_event_ids, required=True),
    ):
        if not is_event_creator(ctx.author):
            await ctx.respond(embed=build_error_embed("You don't have the Event Creator role."), ephemeral=True)
            return
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM events WHERE id=? AND guild_id=?", (event_id, ctx.guild.id)).fetchone()
        if not row:
            await ctx.respond(embed=build_error_embed(f"No event found with ID `{event_id}`."), ephemeral=True)
            return
        await ctx.send_modal(EditEventTimeModal(event=dict(row)))

    @discord.slash_command(name="deleteevent", description="Delete an event permanently.")
    async def deleteevent(
        self,
        ctx: discord.ApplicationContext,
        event_id: discord.Option(int, description="The event ID to delete.", autocomplete=autocomplete_event_ids, required=True),
    ):
        if not is_event_creator(ctx.author):
            await ctx.respond(embed=build_error_embed("You don't have the Event Creator role."), ephemeral=True)
            return
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM events WHERE id=? AND guild_id=?", (event_id, ctx.guild.id)).fetchone()
        if not row:
            await ctx.respond(embed=build_error_embed(f"No event found with ID `{event_id}`."), ephemeral=True)
            return
        event = dict(row)
        embed = discord.Embed(
            title="⚠️  Confirm Deletion",
            description=f"Are you sure you want to permanently delete **{event['title']}** (ID: `{event_id}`)?\n\nThis cannot be undone.",
            color=discord.Color.orange(),
        )
        await ctx.respond(embed=embed, view=DeleteConfirmView(event=event, author_id=ctx.author.id), ephemeral=True)

    @discord.slash_command(name="listevents", description="List all upcoming events in this server.")
    async def listevents(self, ctx: discord.ApplicationContext):
        now = datetime.now(timezone.utc)
        start_iso = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT id, title, start_time, timezone, channel_id, is_recurring, recur_rule FROM events WHERE guild_id=? AND start_time >= ? ORDER BY start_time ASC",
                (ctx.guild.id, start_iso),
            ).fetchall()
        if not rows:
            await ctx.respond(embed=build_error_embed("No upcoming events. Create one with `/newevent`!"), ephemeral=True)
            return
        if len(rows) <= 10:
            await ctx.respond(embed=build_listevents_embed(rows, ctx.guild), ephemeral=True)
        else:
            view = ListEventsView(rows, ctx.guild, ctx.author.id)
            await ctx.respond(embed=view._build_embed(), view=view, ephemeral=True)

    @discord.slash_command(name="eventbuttons", description="Customize RSVP button labels and toggle the Tentative button.")
    async def eventbuttons(
        self,
        ctx: discord.ApplicationContext,
        event_id: discord.Option(int, description="The event ID to customize.", autocomplete=autocomplete_event_ids, required=True),
    ):
        if not is_event_creator(ctx.author):
            await ctx.respond(embed=build_error_embed("You need the **Event Creator** role to customize buttons."), ephemeral=True)
            return
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM events WHERE id=? AND guild_id=?", (event_id, ctx.guild.id)).fetchone()
        if not row:
            await ctx.respond(embed=build_error_embed(f"No event found with ID `{event_id}`."), ephemeral=True)
            return
        await ctx.send_modal(EventButtonsModal(event=dict(row), premium=is_premium(ctx.guild.id)))

    @discord.slash_command(name="cancelevent", description="Soft cancel an event (marks as cancelled without deleting).")
    async def cancelevent(
        self,
        ctx: discord.ApplicationContext,
        event_id: discord.Option(int, description="The event ID to cancel.", autocomplete=autocomplete_event_ids, required=True),
    ):
        if not is_event_creator(ctx.author):
            await ctx.respond(embed=build_error_embed("You don't have the Event Creator role."), ephemeral=True)
            return
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM events WHERE id=? AND guild_id=?", (event_id, ctx.guild.id)).fetchone()
        if not row:
            await ctx.respond(embed=build_error_embed(f"No event found with ID `{event_id}`."), ephemeral=True)
            return
        event = dict(row)
        if event["title"].startswith("[CANCELLED]"):
            await ctx.respond(embed=build_error_embed("This event has already been cancelled."), ephemeral=True)
            return
        with get_connection() as conn:
            conn.execute(
                "UPDATE events SET title=?, description=? WHERE id=?",
                (f"[CANCELLED] {event['title']}", (event.get("description") or "") + "\n\n*This event has been cancelled.*", event_id),
            )
            conn.commit()
        await ctx.respond(embed=build_success_embed(f"Event **{event['title']}** (ID: `{event_id}`) marked as cancelled."), ephemeral=True)
        from cogs.rsvp import refresh_event_embed
        await refresh_event_embed(event_id, ctx.guild, ctx.bot)

    @discord.slash_command(name="myevents", description="List events you've RSVPed to in this server.")
    async def myevents(self, ctx: discord.ApplicationContext):
        now_iso = datetime.now(timezone.utc).isoformat()
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT e.id, e.title, e.start_time, e.timezone, e.channel_id, r.status FROM rsvps r JOIN events e ON r.event_id = e.id WHERE r.user_id=? AND e.guild_id=? AND e.start_time >= ? AND r.status IN ('accepted','tentative') ORDER BY e.start_time ASC",
                (ctx.author.id, ctx.guild.id, now_iso),
            ).fetchall()
        if not rows:
            await ctx.respond(embed=build_error_embed("You haven't RSVPed to any upcoming events in this server."), ephemeral=True)
            return
        embed = discord.Embed(title="📅  My RSVPs", color=COLOR_EVENT)
        for row in rows:
            try:
                tz = pytz.timezone(row["timezone"] or "UTC")
                dt = datetime.fromisoformat(row["start_time"]).astimezone(tz)
                ts = dt.strftime("%b %d, %Y  %I:%M %p %Z")
            except Exception:
                ts = row["start_time"]
            ch = ctx.guild.get_channel(row["channel_id"])
            embed.add_field(
                name=f"{'✅' if row['status'] == 'accepted' else '❓'} {row['title']}",
                value=f"🕐 {ts}  •  {ch.mention if ch else 'unknown channel'}",
                inline=False,
            )
        await ctx.respond(embed=embed, ephemeral=True)

    @discord.slash_command(name="exportevents", description="Export upcoming events as an .ics calendar file.")
    async def exportevents(self, ctx: discord.ApplicationContext):
        now_iso = datetime.now(timezone.utc).isoformat()
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE guild_id=? AND start_time >= ? ORDER BY start_time ASC",
                (ctx.guild.id, now_iso),
            ).fetchall()
        if not rows:
            await ctx.respond(embed=build_error_embed("No upcoming events to export."), ephemeral=True)
            return

        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Soren//Discord Events//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
        for row in rows:
            event = dict(row)
            try:
                s_dt    = datetime.fromisoformat(event["start_time"]).astimezone(pytz.utc)
                dtstart = s_dt.strftime("%Y%m%dT%H%M%SZ")
                dtend   = (datetime.fromisoformat(event["end_time"]).astimezone(pytz.utc).strftime("%Y%m%dT%H%M%SZ") if event.get("end_time") else (s_dt + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ"))
            except Exception:
                continue
            lines += [
                "BEGIN:VEVENT",
                f"UID:soren-event-{event['id']}@{ctx.guild.id}",
                f"SUMMARY:{event['title'].replace(chr(92), chr(92)*2).replace(';', chr(92)+';').replace(',', chr(92)+',')}",
                f"DTSTART:{dtstart}", f"DTEND:{dtend}",
                f"DESCRIPTION:{(event.get('description') or '').replace(chr(10), chr(92)+'n')}",
                "END:VEVENT",
            ]
        lines.append("END:VCALENDAR")
        file = discord.File(io.BytesIO("\r\n".join(lines).encode("utf-8")), filename=f"{ctx.guild.name}_events.ics")
        await ctx.respond(
            embed=build_success_embed(f"Exported **{len(rows)}** upcoming event(s). Import the attached file into any calendar app."),
            file=file, ephemeral=True,
        )


def setup(bot: discord.Bot):
    bot.add_cog(Events(bot))