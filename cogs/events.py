"""
cogs/events.py
===============
Slash commands for creating, editing, and deleting events.

Commands
--------
/newevent          — Create an event via inline slash command options (no modal flow)
/editeventdetails  — Edit title, description, max RSVPs, notify role
/editeventtime     — Edit start/end time, timezone, reminder offset
/deleteevent       — Delete an event (with confirmation)
/listevents        — List all upcoming events in this server
/eventbuttons      — Customize RSVP button labels and toggle tentative
/cancelevent       — Soft cancel an event
/myevents          — List events the caller has RSVPed to
/exportevents      — Export upcoming events as a .ics calendar file

/newevent parameters
--------------------
Required:
  channel     — Text channel to post the event in
  title       — Event title
  start       — Start date/time (flexible format)

Optional:
  description — Event description
  end         — End date/time
  timezone    — tz name with autocomplete (default: UTC)
  recurrence  — none/daily/weekly/biweekly/bimonthly/monthly/custom (default: none)
  role        — Discord role to ping on creation and at reminder time
  reminder    — Minutes before start to send reminder (default: 15)
  max_rsvp    — Maximum accepted RSVPs (default: 0 = unlimited)
  recur_interval — Days between occurrences if recurrence=custom
"""

import discord
from discord.ext import commands
from datetime import datetime, timedelta, timezone
import pytz
import io
import json

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
DEFAULT_REMINDER_OFFSET = 15  # minutes

RECUR_LABELS = {
    "none":      "One-time",
    "daily":     "Daily",
    "weekly":    "Weekly",
    "biweekly":  "Bi-Weekly",
    "bimonthly": "Bi-Monthly",
    "monthly":   "Monthly",
    "custom":    "Custom",
}

# ── Timezone autocomplete list ────────────────────────────────────────────────
TIMEZONE_CHOICES = [
    "America/New_York", "America/Chicago", "America/Denver", "America/Phoenix",
    "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu", "America/Halifax",
    "America/St_Johns", "Europe/London", "Europe/Berlin", "Europe/Paris",
    "Europe/Moscow", "Asia/Dubai", "Asia/Kolkata", "Asia/Bangkok",
    "Asia/Tokyo", "Asia/Shanghai", "Australia/Sydney", "Pacific/Auckland", "UTC",
]


# ── Autocomplete callbacks ────────────────────────────────────────────────────

async def autocomplete_event_ids(ctx: discord.AutocompleteContext):
    now = datetime.now(timezone.utc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, title FROM events WHERE guild_id=? AND start_time >= ? ORDER BY start_time ASC LIMIT 25",
            (ctx.interaction.guild.id, start_of_day)
        ).fetchall()
    return [discord.OptionChoice(name=f"{row['id']}: {row['title'][:80]}", value=row['id']) for row in rows]


async def autocomplete_timezone(ctx: discord.AutocompleteContext):
    val = ctx.value.lower()
    matches = [tz for tz in TIMEZONE_CHOICES if val in tz.lower()]
    # Also search all pytz timezones for more specific queries
    if len(matches) < 10 and len(val) >= 3:
        matches += [tz for tz in pytz.all_timezones if val in tz.lower() and tz not in matches]
    return [discord.OptionChoice(name=tz, value=tz) for tz in matches[:25]]


async def autocomplete_recurrence(ctx: discord.AutocompleteContext):
    options = [
        discord.OptionChoice(name="Single Event (no repeat)", value="none"),
        discord.OptionChoice(name="Daily",                    value="daily"),
        discord.OptionChoice(name="Weekly",                   value="weekly"),
        discord.OptionChoice(name="Bi-Weekly",                value="biweekly"),
        discord.OptionChoice(name="Bi-Monthly",               value="bimonthly"),
        discord.OptionChoice(name="Monthly",                  value="monthly"),
        discord.OptionChoice(name="Custom interval",          value="custom"),
    ]
    val = ctx.value.lower()
    return [o for o in options if val in o.name.lower()] or options


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


def _parse_role_ids(event: dict) -> list[int]:
    """
    Return the list of notify role IDs for an event.
    Reads from notify_role_ids (JSON array, new) first.
    Falls back to notify_role_id (single int, legacy) if notify_role_ids is absent.
    Always returns a plain list of ints (may be empty).
    """
    raw = event.get("notify_role_ids")
    if raw:
        try:
            ids = json.loads(raw)
            if isinstance(ids, list):
                return [int(i) for i in ids if i]
        except Exception:
            pass
    # Legacy fallback
    legacy = event.get("notify_role_id")
    if legacy:
        return [int(legacy)]
    return []


async def post_event_embed(channel: discord.TextChannel, event_data: dict):
    """
    Build and post the event embed.
    Pings all roles in notify_role_ids on creation if set.
    Falls back to legacy notify_role_id for backward compatibility.
    """
    from cogs.rsvp import EventView
    cfg = get_guild_config(channel.guild.id)
    event_data = {**event_data, "embed_color": cfg.get("embed_color") if cfg else None}
    rsvps = {"accepted": [], "declined": [], "tentative": []}
    embed = build_event_embed(event_data, rsvps)
    view  = EventView(event_id=event_data["id"], event=event_data)

    # Build ping string from notify_role_ids (new) or notify_role_id (legacy)
    ping_parts = []
    role_ids = _parse_role_ids(event_data)
    for rid in role_ids:
        role = channel.guild.get_role(rid)
        if role:
            ping_parts.append(role.mention)
    ping_content = " ".join(ping_parts) if ping_parts else None

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


# ── Edit Event Details Modal ──────────────────────────────────────────────────

class EditEventDetailsModal(discord.ui.Modal):
    """
    Edit title, description, max RSVPs, and notify role(s).
    Opened by /editeventdetails.
    Free:    1 role max. Premium: up to 3 roles (comma-separated in one field).
    Fields: Title · Description · Max RSVPs · Mention & Reminder Role(s)  (4 of 5 slots)
    """

    def __init__(self, event: dict, guild: discord.Guild, *args, **kwargs):
        super().__init__(title="Edit Event Details", *args, **kwargs)
        self.event   = event
        self.guild   = guild
        self.premium = is_premium(guild.id)

        # Build pre-fill value from existing roles
        existing_role_ids = _parse_role_ids(event)
        role_names = []
        for rid in existing_role_ids:
            r = guild.get_role(rid)
            if r:
                role_names.append(r.name)
        role_prefill = ", ".join(role_names)

        placeholder = (
            "Up to 3 roles — e.g.  Members, Officers, Raid Team  (leave blank to clear)"
            if self.premium else
            "Role name, @Role, or role ID — leave blank to clear"
        )

        self.add_item(discord.ui.InputText(label="Event Title", value=event["title"], max_length=100))
        self.add_item(discord.ui.InputText(label="Description (optional)", value=event.get("description") or "", style=discord.InputTextStyle.paragraph, required=False, max_length=500))
        self.add_item(discord.ui.InputText(label="Max RSVPs (0 = unlimited)", value=str(event.get("max_rsvp") or 0), max_length=6))
        self.add_item(discord.ui.InputText(
            label="Mention & Reminder Role(s) (optional)",
            value=role_prefill,
            placeholder=placeholder,
            required=False,
            max_length=200,
        ))

    async def callback(self, interaction: discord.Interaction):
        from cogs.rsvp import refresh_event_embed
        await interaction.response.defer(ephemeral=True)

        title        = self.children[0].value.strip()
        description  = self.children[1].value.strip()
        max_rsvp_raw = self.children[2].value.strip()
        roles_raw    = self.children[3].value.strip()

        try:
            max_rsvp = max(0, int(max_rsvp_raw))
        except ValueError:
            max_rsvp = 0

        # Resolve roles — blank clears all
        role_ids = []
        if roles_raw:
            max_roles = 3 if self.premium else 1
            parts = [p.strip() for p in roles_raw.split(",") if p.strip()]
            for part in parts[:max_roles]:
                resolved = None
                try:
                    r = interaction.guild.get_role(int(part))
                    if r:
                        resolved = r.id
                except ValueError:
                    clean = part.lstrip("@").strip()
                    r = discord.utils.find(lambda r: r.name.lower() == clean.lower(), interaction.guild.roles)
                    if r:
                        resolved = r.id
                if resolved:
                    role_ids.append(resolved)

        notify_role_id  = role_ids[0] if role_ids else None
        notify_role_ids = json.dumps(role_ids) if role_ids else None

        with get_connection() as conn:
            conn.execute(
                "UPDATE events SET title=?, description=?, max_rsvp=?, notify_role_id=?, notify_role_ids=? WHERE id=?",
                (title, description, max_rsvp, notify_role_id, notify_role_ids, self.event["id"]),
            )
            conn.commit()

        await interaction.followup.send(embed=build_success_embed(f"Details updated for **{title}**."), ephemeral=True)
        await refresh_event_embed(self.event["id"], interaction.guild, interaction.client)


# ── Edit Event Time Modal ─────────────────────────────────────────────────────

class EditEventTimeModal(discord.ui.Modal):
    """Edit start time, end time, timezone, and reminder offset. Opened by /editeventtime."""

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
    """Core event management commands."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    # ── /newevent ─────────────────────────────────────────────────────────
    @discord.slash_command(name="newevent", description="Create a new event.")
    async def newevent(
        self,
        ctx: discord.ApplicationContext,
        channel: discord.Option(discord.TextChannel, description="Channel to post the event in.", required=True),
        title: discord.Option(str, description="Event title.", required=True, max_length=100),
        start: discord.Option(str, description="Start date & time. e.g. 'July 4 8pm' or '2026-07-04 20:00'", required=True),
        description: discord.Option(str, description="Event description (optional).", required=False, default=None),
        end: discord.Option(str, description="End date & time (optional).", required=False, default=None),
        timezone: discord.Option(str, description="Timezone (default: UTC). Start typing to search.", required=False, default="UTC", autocomplete=autocomplete_timezone),
        recurrence: discord.Option(str, description="How often the event repeats (default: no repeat).", required=False, default="none", autocomplete=autocomplete_recurrence),
        role: discord.Option(discord.Role, description="Role to ping on creation and at reminder time (optional).", required=False, default=None),
        role2: discord.Option(discord.Role, description="Second role to ping — ⭐ Premium only.", required=False, default=None),
        role3: discord.Option(discord.Role, description="Third role to ping — ⭐ Premium only.", required=False, default=None),
        reminder: discord.Option(int, description="Minutes before start to send reminder (default: 15).", required=False, default=15),
        max_rsvp: discord.Option(int, description="Max accepted RSVPs — 0 for unlimited (default: 0).", required=False, default=0),
        recur_interval: discord.Option(int, description="Days between occurrences — only used if recurrence is 'custom'.", required=False, default=7),
    ):
        if not check_setup(ctx.guild.id):
            await ctx.respond(embed=build_error_embed("Run `/setup` first to configure Soren."), ephemeral=True)
            return
        if not is_event_creator(ctx.author):
            await ctx.respond(embed=build_error_embed("You don't have the Event Creator role."), ephemeral=True)
            return

        # Defer immediately — dateparser can take a moment
        await ctx.defer(ephemeral=True)
        guild_id = ctx.guild.id
        premium  = is_premium(guild_id)

        # ── Tier cap check ────────────────────────────────────────────────
        active_count = get_guild_event_count(guild_id)
        if premium and active_count >= PREMIUM_EVENT_LIMIT:
            await ctx.followup.send(
                embed=build_error_embed(
                    f"Premium servers are limited to **{PREMIUM_EVENT_LIMIT} active events**. "
                    f"You currently have **{active_count}** — delete or wait for some to pass."
                ),
                ephemeral=True,
            )
            return
        elif not premium and active_count >= FREE_EVENT_LIMIT:
            await ctx.followup.send(
                embed=build_error_embed(
                    f"Free servers are limited to **{FREE_EVENT_LIMIT} active events** "
                    f"({active_count}/{FREE_EVENT_LIMIT} used). "
                    f"Delete an old event or upgrade to ⭐ Premium for up to {PREMIUM_EVENT_LIMIT}."
                ),
                ephemeral=True,
            )
            return

        # ── Validate timezone ─────────────────────────────────────────────
        tz_name = timezone or "UTC"
        if tz_name not in pytz.all_timezones:
            await ctx.followup.send(
                embed=build_error_embed(
                    f"Unknown timezone: `{tz_name}`.\n"
                    "Start typing in the timezone field to see suggestions, "
                    "or use a full tz name like `America/New_York`."
                ),
                ephemeral=True,
            )
            return

        # ── Validate recurrence ───────────────────────────────────────────
        valid_recurrences = {"none", "daily", "weekly", "biweekly", "bimonthly", "monthly", "custom"}
        recur_rule = (recurrence or "none").lower()
        if recur_rule not in valid_recurrences:
            recur_rule = "none"

        # ── Parse start time ──────────────────────────────────────────────
        start_dt = _parse_datetime(start, tz_name)
        if not start_dt:
            await ctx.followup.send(
                embed=build_error_embed(
                    "Couldn't parse the start date/time.\n"
                    "Try formats like `July 4 8pm`, `next Friday 9pm`, or `2026-07-04 20:00`."
                ),
                ephemeral=True,
            )
            return
        start_iso = start_dt.isoformat()

        # ── Parse end time ────────────────────────────────────────────────
        end_iso = None
        if end:
            end_dt = _parse_datetime(end, tz_name)
            if not end_dt:
                await ctx.followup.send(
                    embed=build_error_embed("Couldn't parse the end date/time."),
                    ephemeral=True,
                )
                return
            end_iso = end_dt.isoformat()

        is_recurring    = 0 if recur_rule == "none" else 1
        reminder_offset = max(1, reminder) if reminder else DEFAULT_REMINDER_OFFSET
        recur_int       = max(1, recur_interval) if recur_interval else 7
        desc_clean      = (description or "").strip()
        max_rsvp_clean  = max(0, max_rsvp) if max_rsvp else 0

        # ── Build role list ───────────────────────────────────────────────
        # Free: max 1 role. Premium: up to 3 roles.
        # Extra roles (role2, role3) are silently ignored for free servers.
        role_ids = []
        if role:
            role_ids.append(role.id)
        if premium:
            if role2:
                role_ids.append(role2.id)
            if role3:
                role_ids.append(role3.id)
        elif (role2 or role3):
            log.info(
                f"role2/role3 supplied by non-premium guild {guild_id} — ignoring extra roles"
            )

        # Keep legacy notify_role_id in sync with the first role for backward compat
        notify_role_id  = role_ids[0] if role_ids else None
        notify_role_ids = json.dumps(role_ids) if role_ids else None

        tz_warning = "\n⚠️ No timezone specified — event created in **UTC**. Use `/editeventtime` to change if needed." if tz_name == "UTC" and not timezone else ""

        # ── Insert into DB ────────────────────────────────────────────────
        with get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO events
                    (guild_id, channel_id, creator_id, title, description,
                     timezone, start_time, end_time, is_recurring,
                     recur_rule, recur_interval, reminder_offset,
                     notify_role_id, notify_role_ids, max_rsvp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (guild_id, channel.id, ctx.author.id,
                 title, desc_clean, tz_name, start_iso, end_iso,
                 is_recurring, recur_rule, recur_int,
                 reminder_offset, notify_role_id, notify_role_ids, max_rsvp_clean),
            )
            event_id = cursor.lastrowid
            conn.commit()

        event = {
            "id": event_id, "title": title, "description": desc_clean,
            "timezone": tz_name, "start_time": start_iso, "end_time": end_iso,
            "is_recurring": is_recurring, "recur_rule": recur_rule,
            "recur_interval": recur_int, "channel_id": channel.id,
            "reminder_offset": reminder_offset,
            "notify_role_id": notify_role_id,
            "notify_role_ids": notify_role_ids,
            "max_rsvp": max_rsvp_clean,
            "btn_accept_label": "✅ Accept", "btn_decline_label": "❌ Decline",
            "btn_tentative_label": "❓ Tentative", "btn_tentative_enabled": 1,
        }

        await post_event_embed(channel, event)

        recur_str = RECUR_LABELS.get(recur_rule, recur_rule)
        if recur_rule == "custom":
            recur_str = f"Every {recur_int} days"

        roles_mentioned = [r for r in [role, role2 if premium else None, role3 if premium else None] if r]
        role_str = f" · pinging {', '.join(r.mention for r in roles_mentioned)}" if roles_mentioned else ""
        log.info(f"Event created: '{title}' (ID {event_id}) in guild {guild_id} by {ctx.author}")

        await ctx.followup.send(
            embed=build_success_embed(
                f"**{title}** created in {channel.mention}! "
                f"(ID: `{event_id}` · {recur_str}{role_str}){tz_warning}"
            ),
            ephemeral=True,
        )

    # ── /editeventdetails ─────────────────────────────────────────────────
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

    # ── /editeventtime ────────────────────────────────────────────────────
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

    # ── /deleteevent ──────────────────────────────────────────────────────
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

    # ── /listevents ───────────────────────────────────────────────────────
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

    # ── /eventbuttons ─────────────────────────────────────────────────────
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

    # ── /cancelevent ──────────────────────────────────────────────────────
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

    # ── /myevents ─────────────────────────────────────────────────────────
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

    # ── /exportevents ─────────────────────────────────────────────────────
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