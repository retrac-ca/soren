"""
cogs/events.py
===============
Slash commands for creating, editing, and deleting events.

Commands
--------
/newevent          — Create event (inline slash command, 14 parameters)
/editeventdetails  — Edit title, description, max RSVPs
/editeventtime     — Edit start/end time, timezone, reminder offset
/editeventmentions — Update or clear mention/reminder roles
/deleteevent       — Delete an event (with confirmation)
/listevents        — List all upcoming events in this server
/eventbuttons      — Customize RSVP button labels and toggle tentative
/cancelevent       — Soft cancel an event
/myevents          — List events the caller has RSVPed to
/exportevents      — Export upcoming events as a .ics calendar file
"""

import discord
from discord.ext import commands
from datetime import datetime, timedelta, timezone
import pytz
import io

from utils.database import get_connection, is_premium, get_guild_config
from utils.permissions import is_event_creator, check_setup
from utils.embeds import build_event_embed, build_error_embed, build_success_embed, get_guild_color, COLOR_EVENT
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
    Tries strict YYYY-MM-DD HH:MM first, then falls back to dateparser.
    Returns a timezone-aware datetime or None if parsing fails.
    """
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


# ── Try to import relativedelta for monthly math ──────────────────────────────
try:
    from dateutil.relativedelta import relativedelta
    HAS_DATEUTIL = True
except ImportError:
    HAS_DATEUTIL = False

# ── Tier event limits ─────────────────────────────────────────────────────────
FREE_EVENT_LIMIT    = 10
PREMIUM_EVENT_LIMIT = 50

# ── RSVP cooldown tracking (in-memory, per user per event) ───────────────────
# key: (user_id, event_id) → timestamp of last interaction
_rsvp_cooldowns: dict[tuple, datetime] = {}
RSVP_COOLDOWN_SECONDS = 3

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


# ── Autocomplete helpers ──────────────────────────────────────────────────────

async def autocomplete_event_ids(ctx: discord.AutocompleteContext):
    """Autocomplete for event IDs, showing ID: Title. Includes events from today onwards."""
    now = datetime.now(timezone.utc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, title FROM events WHERE guild_id=? AND start_time >= ? ORDER BY start_time ASC LIMIT 25",
            (ctx.interaction.guild.id, start_of_day)
        ).fetchall()
    return [discord.OptionChoice(name=f"{row['id']}: {row['title'][:80]}", value=row['id']) for row in rows]


async def autocomplete_timezones(ctx: discord.AutocompleteContext):
    """Autocomplete for timezone — filters TIMEZONES list by what the user has typed."""
    typed = ctx.value.lower()
    results = []
    for opt in TIMEZONES:
        if typed in opt.label.lower() or typed in opt.value.lower():
            results.append(discord.OptionChoice(name=opt.label, value=opt.value))
    return results[:25]


async def autocomplete_recurrence(ctx: discord.AutocompleteContext):
    """Autocomplete for recurrence rule."""
    typed = ctx.value.lower()
    choices = [
        discord.OptionChoice(name="Single Event (no repeat)",           value="none"),
        discord.OptionChoice(name="Daily",                               value="daily"),
        discord.OptionChoice(name="Weekly",                              value="weekly"),
        discord.OptionChoice(name="Bi-Weekly (every 2 weeks)",          value="biweekly"),
        discord.OptionChoice(name="Bi-Monthly (every 2 months)",        value="bimonthly"),
        discord.OptionChoice(name="Monthly",                             value="monthly"),
        discord.OptionChoice(name="Custom interval (set recur_interval)", value="custom"),
    ]
    if not typed:
        return choices
    return [c for c in choices if typed in c.name.lower()]


async def autocomplete_reminder(ctx: discord.AutocompleteContext):
    """Autocomplete for reminder offset — common values shown, user can type any number."""
    suggestions = [
        discord.OptionChoice(name="15 minutes before", value=15),
        discord.OptionChoice(name="30 minutes before", value=30),
        discord.OptionChoice(name="45 minutes before", value=45),
        discord.OptionChoice(name="1 hour before",     value=60),
        discord.OptionChoice(name="2 hours before",    value=120),
        discord.OptionChoice(name="3 hours before",    value=180),
        discord.OptionChoice(name="1 day before",      value=1440),
    ]
    typed = ctx.value.strip()
    if not typed:
        return suggestions
    try:
        val = int(typed)
        matching = [s for s in suggestions if str(s.value).startswith(typed)]
        if not matching:
            return [discord.OptionChoice(name=f"{val} minutes before", value=val)]
        return matching
    except ValueError:
        return [s for s in suggestions if typed.lower() in s.name.lower()]


# ── Misc helpers ──────────────────────────────────────────────────────────────

def build_listevents_embed(rows, guild):
    cfg   = get_guild_config(guild.id)
    color = get_guild_color(cfg.get("embed_color") if cfg else None)
    embed = discord.Embed(title="📅  Upcoming Events", color=color)
    for row in rows:
        try:
            tz = pytz.timezone(row["timezone"] or "UTC")
            dt = datetime.fromisoformat(row["start_time"]).astimezone(tz)
            ts = dt.strftime("%b %d, %Y  %I:%M %p %Z")
        except Exception:
            ts = row["start_time"]
        recur_tag = f"  🔁 {RECUR_LABELS.get(row['recur_rule'], row['recur_rule'])}" if row["is_recurring"] else ""
        ch = guild.get_channel(row["channel_id"])
        ch_str = ch.mention if ch else "unknown channel"
        embed.add_field(
            name=f"[ID: {row['id']}]  {row['title']}{recur_tag}",
            value=f"🕐 {ts}  •  {ch_str}",
            inline=False,
        )
    return embed


def get_guild_event_count(guild_id: int) -> int:
    """Count only upcoming non-cancelled events for tier cap purposes.
    Past events and cancelled events don't count against the limit."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM events WHERE guild_id = ? AND start_time >= ? AND title NOT LIKE '[CANCELLED]%'",
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


async def post_event_embed(channel: discord.TextChannel, event_data: dict, bot: discord.Bot | None = None):
    """Build and post the event embed. Sends role ping then embed. Saves message_id back to DB."""
    import json as _json
    from cogs.rsvp import EventView

    cfg = get_guild_config(channel.guild.id)
    event_data = {**event_data, "embed_color": cfg.get("embed_color") if cfg else None}

    # ── Role ping — fires before the embed so it appears above it ────────
    ping_parts = []
    raw = event_data.get("notify_role_ids")
    if raw:
        try:
            ids = _json.loads(raw)
            for rid in ids:
                role = channel.guild.get_role(int(rid))
                if role:
                    ping_parts.append(role.mention)
        except Exception:
            pass
    # Legacy single-role fallback
    if not ping_parts and event_data.get("notify_role_id"):
        role = channel.guild.get_role(int(event_data["notify_role_id"]))
        if role:
            ping_parts.append(role.mention)

    if ping_parts:
        try:
            await channel.send(" ".join(ping_parts))
        except discord.Forbidden:
            log.warning(f"post_event_embed: no permission to send role ping in channel {channel.id}")

    rsvps = {"accepted": [], "declined": [], "tentative": []}
    embed = build_event_embed(event_data, rsvps)
    view  = EventView(event_id=event_data["id"], event=event_data)
    msg   = await channel.send(embed=embed, view=view)
    with get_connection() as conn:
        conn.execute("UPDATE events SET message_id = ? WHERE id = ?", (msg.id, event_data["id"]))
        conn.commit()

    # ── Modlog: event created ─────────────────────────────────────────────
    try:
        from cogs.modlogs import log_event, embed_event_created
        creator = channel.guild.get_member(event_data.get("creator_id", 0))
        if bot is None:
            log.warning(f"modlog hook (event created): bot reference is None — skipping modlog for event {event_data.get('id')}")
        elif creator is None:
            log.warning(f"modlog hook (event created): creator member not found (id={event_data.get('creator_id')}) — skipping modlog for event {event_data.get('id')}")
        else:
            ml_embed = embed_event_created(event_data, creator)
            await log_event(bot, channel.guild.id, ml_embed)
    except Exception as e:
        log.warning(f"modlog hook failed (event created): {e}")


async def repost_recurring_embed(bot: discord.Bot, event_id: int):
    """
    Called after a recurring event advances to its next occurrence.
    Deletes the old embed and posts a fresh one with the updated time.
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

    # Delete the old embed
    if event.get("message_id"):
        try:
            old_msg = await channel.fetch_message(event["message_id"])
            await old_msg.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

    # Post a fresh embed — ping roles first so the mention appears above the embed
    cfg   = get_guild_config(guild.id)
    event = {**event, "embed_color": cfg.get("embed_color") if cfg else None}

    # ── Role ping ─────────────────────────────────────────────────────────
    import json as _json
    ping_parts = []
    raw = event.get("notify_role_ids")
    if raw:
        try:
            for rid in _json.loads(raw):
                role = channel.guild.get_role(int(rid))
                if role:
                    ping_parts.append(role.mention)
        except Exception:
            pass
    if not ping_parts and event.get("notify_role_id"):
        role = channel.guild.get_role(int(event["notify_role_id"]))
        if role:
            ping_parts.append(role.mention)
    if ping_parts:
        try:
            await channel.send(" ".join(ping_parts))
        except discord.Forbidden:
            pass

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
    Edit title, description, max RSVPs, and notify role.
    Opened by /editeventdetails.
    Fields: Title · Description · Max RSVPs · Notify Role  (4 of 5 slots used)
    """

    def __init__(self, event: dict, guild: discord.Guild, *args, **kwargs):
        super().__init__(title="Edit Event Details", *args, **kwargs)
        self.event = event

        # Resolve current notify role name for pre-fill
        notify_role_value = ""
        if event.get("notify_role_id"):
            role = guild.get_role(event["notify_role_id"])
            if role:
                notify_role_value = role.name

        self.add_item(discord.ui.InputText(
            label="Event Title",
            value=event["title"],
            max_length=100,
        ))
        self.add_item(discord.ui.InputText(
            label="Description (optional)",
            value=event.get("description") or "",
            style=discord.InputTextStyle.paragraph,
            required=False,
            max_length=500,
        ))
        self.add_item(discord.ui.InputText(
            label="Max RSVPs (0 = unlimited)",
            value=str(event.get("max_rsvp") or 0),
            max_length=6,
        ))
        self.add_item(discord.ui.InputText(
            label="Notify Role (optional)",
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

        # Resolve notify role — keep existing if field left blank
        notify_role_id = self.event.get("notify_role_id")
        if role_raw:
            try:
                role = interaction.guild.get_role(int(role_raw))
                if role:
                    notify_role_id = role.id
            except ValueError:
                clean = role_raw.lstrip("@").strip()
                role = discord.utils.find(lambda r: r.name.lower() == clean.lower(), interaction.guild.roles)
                notify_role_id = role.id if role else notify_role_id

        with get_connection() as conn:
            conn.execute(
                "UPDATE events SET title=?, description=?, max_rsvp=?, notify_role_id=? WHERE id=?",
                (title, description, max_rsvp, notify_role_id, self.event["id"]),
            )
            conn.commit()

        await interaction.followup.send(
            embed=build_success_embed(f"Details updated for **{title}**."),
            ephemeral=True,
        )
        await refresh_event_embed(self.event["id"], interaction.guild, interaction.client)

        # ── Modlog: event edited ──────────────────────────────────────────
        try:
            from cogs.modlogs import log_event, embed_event_edited
            ml_embed = embed_event_edited(
                {**self.event, "title": title},
                interaction.user,
                "Title, description, max RSVPs, or notify role",
            )
            await log_event(interaction.client, interaction.guild_id, ml_embed)
        except Exception as e:
            log.warning(f"modlog hook failed (editeventdetails): {e}")


# ── Edit Event Time Modal ─────────────────────────────────────────────────────

class EditEventTimeModal(discord.ui.Modal):
    """
    Edit start time, end time, timezone, and reminder offset.
    Opened by /editeventtime.
    Fields: Start · End · Timezone · Reminder  (4 of 5 slots used)
    """

    def __init__(self, event: dict, *args, **kwargs):
        super().__init__(title="Edit Event Time", *args, **kwargs)
        self.event = event

        self.add_item(discord.ui.InputText(
            label="Start Date & Time",
            value=event["start_time"][:16].replace("T", " "),
            placeholder="2026-07-04 20:00  or  July 4 8pm",
            max_length=50,
        ))
        self.add_item(discord.ui.InputText(
            label="End Date & Time (optional)",
            value=event.get("end_time", "")[:16].replace("T", " ") if event.get("end_time") else "",
            placeholder="Leave blank for no end time",
            required=False,
            max_length=50,
        ))
        self.add_item(discord.ui.InputText(
            label="Timezone",
            value=event.get("timezone") or "UTC",
            placeholder="e.g. America/New_York, UTC, Europe/London",
            max_length=50,
        ))
        self.add_item(discord.ui.InputText(
            label="Reminder (minutes before start)",
            value=str(event.get("reminder_offset") or 15),
            max_length=4,
        ))

    async def callback(self, interaction: discord.Interaction):
        from cogs.rsvp import refresh_event_embed

        # Defer immediately — dateparser can exceed Discord's 3s timeout,
        # and deferring also lets us send proper error embeds via followup
        await interaction.response.defer(ephemeral=True)

        start_raw    = self.children[0].value.strip()
        end_raw      = self.children[1].value.strip()
        tz_name      = self.children[2].value.strip() or "UTC"
        reminder_raw = self.children[3].value.strip()

        if tz_name not in pytz.all_timezones:
            await interaction.followup.send(
                embed=build_error_embed(
                    f"Unknown timezone: `{tz_name}`.\n"
                    "Use a standard tz name like `America/New_York`, `Europe/London`, or `UTC`.\n"
                    "Full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
                ),
                ephemeral=True,
            )
            return

        try:
            reminder = max(1, int(reminder_raw))
        except ValueError:
            reminder = 15

        start_dt = _parse_datetime(start_raw, tz_name)
        if not start_dt:
            await interaction.followup.send(
                embed=build_error_embed(
                    "Couldn't parse the start date/time. Try `2026-07-04 20:00` or `July 4 8pm`."
                ),
                ephemeral=True,
            )
            return
        start_iso = start_dt.isoformat()

        end_iso = None
        if end_raw:
            end_dt = _parse_datetime(end_raw, tz_name)
            if not end_dt:
                await interaction.followup.send(
                    embed=build_error_embed("Couldn't parse the end date/time."),
                    ephemeral=True,
                )
                return
            end_iso = end_dt.isoformat()

        with get_connection() as conn:
            conn.execute(
                "UPDATE events SET start_time=?, end_time=?, timezone=?, reminder_offset=?, reminded_at=NULL WHERE id=?",
                (start_iso, end_iso, tz_name, reminder, self.event["id"]),
            )
            conn.commit()

        await interaction.followup.send(
            embed=build_success_embed(f"Time updated for **{self.event['title']}**."),
            ephemeral=True,
        )
        await refresh_event_embed(self.event["id"], interaction.guild, interaction.client)

        # ── Modlog: event edited ──────────────────────────────────────────
        try:
            from cogs.modlogs import log_event, embed_event_edited
            ml_embed = embed_event_edited(
                self.event,
                interaction.user,
                "Start time, end time, timezone, or reminder offset",
            )
            await log_event(interaction.client, interaction.guild_id, ml_embed)
        except Exception as e:
            log.warning(f"modlog hook failed (editeventtime): {e}")


# ── Event Buttons Modal ───────────────────────────────────────────────────────

class EventButtonsModal(discord.ui.Modal):
    """
    Free servers: toggle Tentative button on/off only.
    Premium servers: toggle Tentative + set custom labels for all three buttons.

    Free modal  (2 of 5 slots): Show Tentative · Accept Label (read-only hint)
    Premium modal (4 of 5 slots): Show Tentative · Accept Label · Tentative Label · Decline Label
    """

    def __init__(self, event: dict, premium: bool, *args, **kwargs):
        super().__init__(title="Button Settings", *args, **kwargs)
        self.event   = event
        self.premium = premium

        current = "yes" if event.get("btn_tentative_enabled", 1) else "no"

        # Field 1 — always shown (free + premium)
        self.add_item(discord.ui.InputText(
            label="Show Tentative Button? (yes / no)",
            value=current,
            max_length=3,
        ))

        if premium:
            # Fields 2–4 — custom labels (premium only)
            self.add_item(discord.ui.InputText(
                label="Accept Button Label",
                value=event.get("btn_accept_label") or "✅ Accept",
                max_length=20,
            ))
            self.add_item(discord.ui.InputText(
                label="Tentative Button Label",
                value=event.get("btn_tentative_label") or "❓ Tentative",
                max_length=20,
            ))
            self.add_item(discord.ui.InputText(
                label="Decline Button Label",
                value=event.get("btn_decline_label") or "❌ Decline",
                max_length=20,
            ))
        else:
            # Show a read-only hint so free users know labels exist as a premium feature
            self.add_item(discord.ui.InputText(
                label="Custom Labels (⭐ Premium only)",
                value="Upgrade to Premium to set custom button labels.",
                required=False,
                max_length=50,
            ))

    async def callback(self, interaction: discord.Interaction):
        from cogs.rsvp import refresh_event_embed

        show_raw       = self.children[0].value.strip().lower()
        show_tentative = 0 if show_raw in ("no", "n", "false", "0") else 1

        if self.premium:
            accept_label    = self.children[1].value.strip() or "✅ Accept"
            tentative_label = self.children[2].value.strip() or "❓ Tentative"
            decline_label   = self.children[3].value.strip() or "❌ Decline"
        else:
            # Keep existing labels unchanged for free servers
            accept_label    = self.event.get("btn_accept_label")    or "✅ Accept"
            tentative_label = self.event.get("btn_tentative_label") or "❓ Tentative"
            decline_label   = self.event.get("btn_decline_label")   or "❌ Decline"

        with get_connection() as conn:
            conn.execute(
                "UPDATE events SET btn_tentative_enabled=?, btn_accept_label=?, btn_tentative_label=?, btn_decline_label=? WHERE id=?",
                (show_tentative, accept_label, tentative_label, decline_label, self.event["id"]),
            )
            conn.commit()

        state = "visible" if show_tentative else "hidden"
        label_note = " Labels updated." if self.premium else ""
        await interaction.response.send_message(
            embed=build_success_embed(
                f"Updated **{self.event['title']}** — Tentative button is now **{state}**.{label_note}"
            ),
            ephemeral=True,
        )
        await refresh_event_embed(self.event["id"], interaction.guild, interaction.client)


# ── Delete confirmation view ──────────────────────────────────────────────────

class DeleteConfirmView(discord.ui.View):
    """Two-button confirmation before permanently deleting an event."""

    def __init__(self, event: dict, author_id: int):
        super().__init__(timeout=60)
        self.event     = event
        self.author_id = author_id
        self.message   = None   # Set after respond so on_timeout can edit it

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the person who ran this command can confirm.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, delete it", style=discord.ButtonStyle.danger)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.stop()
        event = self.event

        try:
            guild = interaction.guild
            ch    = guild.get_channel(event["channel_id"])
            if ch and event.get("message_id"):
                msg = await ch.fetch_message(event["message_id"])
                await msg.delete()
        except discord.NotFound:
            pass

        with get_connection() as conn:
            conn.execute("DELETE FROM events WHERE id=?", (event["id"],))
            conn.commit()

        # ── Modlog: event deleted ─────────────────────────────────────────
        try:
            from cogs.modlogs import log_event, embed_event_deleted
            ml_embed = embed_event_deleted(event, interaction.user)
            await log_event(interaction.client, interaction.guild_id, ml_embed)
        except Exception as e:
            log.warning(f"modlog hook failed (event deleted): {e}")

        await interaction.response.edit_message(
            embed=build_success_embed(f"Event **{event['title']}** (ID: `{event['id']}`) has been deleted."),
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(description="❎  Deletion cancelled.", color=discord.Color.blurple()),
            view=None,
        )

    async def on_timeout(self):
        """Disable buttons silently when the view times out."""
        for item in self.children:
            item.disabled = True


# ── Waitlist RSVP view ────────────────────────────────────────────────────────

class WaitlistView(discord.ui.View):
    """Shown when an event is full. Offers to join the waitlist."""

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
            conn.execute(
                "INSERT INTO waitlist (event_id, user_id) VALUES (?, ?)",
                (self.event_id, interaction.user.id),
            )
            conn.commit()

        # Get position
        with get_connection() as conn:
            pos = conn.execute(
                "SELECT COUNT(*) as cnt FROM waitlist WHERE event_id=? AND user_id <= (SELECT id FROM waitlist WHERE event_id=? AND user_id=?)",
                (self.event_id, self.event_id, interaction.user.id),
            ).fetchone()["cnt"]

        await interaction.response.send_message(
            f"✅ You've been added to the waitlist! You are **#{pos}** in line.",
            ephemeral=True,
        )


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
        # Use guild custom color — consistent with /listevents single-page path
        cfg   = get_guild_config(self.guild.id)
        color = get_guild_color(cfg.get("embed_color") if cfg else None)
        embed = discord.Embed(title="📅  Upcoming Events", color=color)
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


# ── Edit Event Mentions Modal ─────────────────────────────────────────────────

class EditEventMentionsModal(discord.ui.Modal):
    """
    Update or clear the notify role(s) for an event.
    Free: one role. Premium: up to three roles (comma-separated).
    Type 'clear' or 'none' to remove all roles.
    Opened by /editeventmentions.
    """

    def __init__(self, event: dict, guild: discord.Guild, premium: bool, *args, **kwargs):
        super().__init__(title="Edit Mention Roles", *args, **kwargs)
        self.event   = event
        self.premium = premium

        import json as _json
        # Pre-fill current roles as comma-separated role names
        current_roles = ""
        raw = event.get("notify_role_ids")
        if raw:
            try:
                ids = _json.loads(raw)
                names = []
                for rid in ids:
                    role = guild.get_role(int(rid))
                    if role:
                        names.append(role.name)
                current_roles = ", ".join(names)
            except Exception:
                pass
        if not current_roles and event.get("notify_role_id"):
            role = guild.get_role(int(event["notify_role_id"]))
            if role:
                current_roles = role.name

        if premium:
            placeholder = "Up to 3 role names, comma-separated. Leave blank or type 'clear' to remove all roles."
        else:
            placeholder = "Role name or ID. Leave blank or type 'clear' to remove the role."

        self.add_item(discord.ui.InputText(
            label="Notify Role(s)" if premium else "Notify Role",
            value=current_roles,
            placeholder=placeholder,
            required=False,
            max_length=200,
        ))

    async def callback(self, interaction: discord.Interaction):
        import json as _json
        from cogs.rsvp import refresh_event_embed

        await interaction.response.defer(ephemeral=True)

        raw_input = self.children[0].value.strip()

        # Treat 'clear', 'none', '-', or blank as explicit clear signals
        is_clear_keyword = raw_input.lower() in ("clear", "none", "-", "")
        role_ids = []
        if raw_input and not is_clear_keyword:
            # Split by comma, resolve each entry to a role
            parts = [p.strip() for p in raw_input.split(",") if p.strip()]
            # Free servers only get the first role
            if not self.premium:
                parts = parts[:1]
            else:
                parts = parts[:3]

            for part in parts:
                clean = part.lstrip("@").strip()
                # Try by ID first, then by name
                role = None
                try:
                    role = interaction.guild.get_role(int(clean))
                except ValueError:
                    role = discord.utils.find(
                        lambda r: r.name.lower() == clean.lower(),
                        interaction.guild.roles,
                    )
                if role:
                    role_ids.append(role.id)
                else:
                    log.warning(f"editeventmentions: could not find role '{part}' in guild {interaction.guild.id}")

        notify_role_ids_json  = _json.dumps(role_ids) if role_ids else None
        notify_role_id_legacy = role_ids[0] if role_ids else None

        with get_connection() as conn:
            conn.execute(
                "UPDATE events SET notify_role_id=?, notify_role_ids=? WHERE id=?",
                (notify_role_id_legacy, notify_role_ids_json, self.event["id"]),
            )
            conn.commit()

        if role_ids:
            role_names = []
            for rid in role_ids:
                r = interaction.guild.get_role(rid)
                role_names.append(r.mention if r else f"<@&{rid}>")
            msg = f"Mention roles updated to: {', '.join(role_names)}"
        else:
            msg = "Mention roles cleared — no roles will be pinged for this event."

        await interaction.followup.send(
            embed=build_success_embed(msg),
            ephemeral=True,
        )
        await refresh_event_embed(self.event["id"], interaction.guild, interaction.client)

        # ── Modlog: event edited ──────────────────────────────────────────
        try:
            from cogs.modlogs import log_event, embed_event_edited
            ml_embed = embed_event_edited(
                self.event,
                interaction.user,
                "Mention/reminder roles",
            )
            await log_event(interaction.client, interaction.guild_id, ml_embed)
        except Exception as e:
            log.warning(f"modlog hook failed (editeventmentions): {e}")


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
        channel: discord.Option(
            discord.TextChannel,
            description="Channel to post the event in.",
            required=True,
        ),
        title: discord.Option(
            str,
            description="Event title.",
            required=True,
            max_length=100,
        ),
        start: discord.Option(
            str,
            description="Start date & time. e.g. 2026-07-04 20:00 or 'July 4 8pm'",
            required=True,
        ),
        description: discord.Option(
            str,
            description="Event description (optional).",
            required=False,
            default=None,
            max_length=500,
        ),
        end: discord.Option(
            str,
            description="End date & time (optional). e.g. 2026-07-04 21:00",
            required=False,
            default=None,
        ),
        timezone: discord.Option(
            str,
            description="Timezone (optional). Defaults to UTC if not set.",
            required=False,
            default=None,
            autocomplete=autocomplete_timezones,
        ),
        recurrence: discord.Option(
            str,
            description="How often the event repeats (optional). Defaults to single event.",
            required=False,
            default="none",
            autocomplete=autocomplete_recurrence,
        ),
        reminder: discord.Option(
            int,
            description="Minutes before start to send reminder (optional). e.g. 15, 60, 1440",
            required=False,
            default=15,
            autocomplete=autocomplete_reminder,
        ),
        max_rsvp: discord.Option(
            int,
            description="Max number of accepted RSVPs (optional). 0 = unlimited.",
            required=False,
            default=0,
        ),
        recur_interval: discord.Option(
            int,
            description="Days between occurrences. Only used when recurrence=custom.",
            required=False,
            default=7,
        ),
        role: discord.Option(
            discord.Role,
            description="Role to ping when event is created and for reminders (optional).",
            required=False,
            default=None,
        ),
        role2: discord.Option(
            discord.Role,
            description="Second role to ping (optional). ⭐ Premium only.",
            required=False,
            default=None,
        ),
        role3: discord.Option(
            discord.Role,
            description="Third role to ping (optional). ⭐ Premium only.",
            required=False,
            default=None,
        ),
    ):
        # ── Guards ────────────────────────────────────────────────────────
        if not check_setup(ctx.guild.id):
            await ctx.respond(embed=build_error_embed("Run `/setup` first to configure Soren."), ephemeral=True)
            return
        if not is_event_creator(ctx.author):
            await ctx.respond(embed=build_error_embed("You don't have the Event Creator role."), ephemeral=True)
            return

        await ctx.defer(ephemeral=True)

        guild_id = ctx.guild.id
        premium  = is_premium(guild_id)

        # ── Tier cap ──────────────────────────────────────────────────────
        if not premium and get_guild_event_count(guild_id) >= FREE_EVENT_LIMIT:
            await ctx.followup.send(
                embed=build_error_embed(
                    f"Free servers are limited to **{FREE_EVENT_LIMIT} active events**. "
                    "Delete or cancel an old event, or upgrade to Premium."
                ),
                ephemeral=True,
            )
            return

        # ── Timezone ──────────────────────────────────────────────────────
        tz_name    = timezone  # None if not provided
        tz_warning = ""
        if tz_name is None:
            tz_name    = "UTC"
            tz_warning = "\n⚠️ No timezone selected — event created in **UTC**. Use `/editeventtime` to fix this."

        if tz_name not in pytz.all_timezones:
            await ctx.followup.send(
                embed=build_error_embed(
                    f"Unknown timezone: `{tz_name}`.\n"
                    "Pick from the autocomplete list or use a standard tz name like `America/New_York`."
                ),
                ephemeral=True,
            )
            return

        # ── Parse start time ──────────────────────────────────────────────
        start_dt = _parse_datetime(start, tz_name)
        if not start_dt:
            await ctx.followup.send(
                embed=build_error_embed(
                    "Couldn't parse the start date/time. "
                    "Try `2026-07-04 20:00`, `July 4 8pm`, or `next Friday 9pm`."
                ),
                ephemeral=True,
            )
            return
        start_iso = start_dt.isoformat()

        # ── Parse end time (optional) ─────────────────────────────────────
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

        # ── Recurrence ────────────────────────────────────────────────────
        recur_rule   = recurrence or "none"
        is_recurring = 0 if recur_rule == "none" else 1
        # recur_interval only meaningful for custom; silently ignore otherwise
        interval = recur_interval if recur_rule == "custom" else 7

        # ── Reminder ──────────────────────────────────────────────────────
        reminder_offset = max(1, reminder) if reminder else 15

        # ── Roles — build notify_role_ids JSON array ──────────────────────
        # Free servers: only role is used; role2/role3 are silently dropped
        # Premium servers: all three accepted
        role_ids      = []
        extra_ignored = False

        if role:
            role_ids.append(role.id)
        if role2:
            if premium:
                role_ids.append(role2.id)
            else:
                extra_ignored = True
        if role3:
            if premium:
                role_ids.append(role3.id)
            else:
                extra_ignored = True

        import json
        notify_role_ids_json = json.dumps(role_ids) if role_ids else None
        # Keep legacy single-role column populated for backward compat
        notify_role_id_legacy = role_ids[0] if role_ids else None

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
                (
                    guild_id, channel.id, ctx.author.id,
                    title.strip(), (description or "").strip(),
                    tz_name, start_iso, end_iso,
                    is_recurring, recur_rule, interval, reminder_offset,
                    notify_role_id_legacy, notify_role_ids_json,
                    max(0, max_rsvp),
                ),
            )
            event_id = cursor.lastrowid
            conn.commit()

        event = {
            "id":                  event_id,
            "guild_id":            guild_id,
            "creator_id":          ctx.author.id,
            "title":               title.strip(),
            "description":         (description or "").strip(),
            "timezone":            tz_name,
            "start_time":          start_iso,
            "end_time":            end_iso,
            "is_recurring":        is_recurring,
            "recur_rule":          recur_rule,
            "recur_interval":      interval,
            "channel_id":          channel.id,
            "reminder_offset":     reminder_offset,
            "notify_role_id":      notify_role_id_legacy,
            "notify_role_ids":     notify_role_ids_json,
            "max_rsvp":            max(0, max_rsvp),
            "btn_accept_label":    "✅ Accept",
            "btn_decline_label":   "❌ Decline",
            "btn_tentative_label": "❓ Tentative",
            "btn_tentative_enabled": 1,
        }

        await post_event_embed(channel, event, bot=ctx.bot)

        # ── Success message ───────────────────────────────────────────────
        recur_str = RECUR_LABELS.get(recur_rule, recur_rule)
        if recur_rule == "custom":
            recur_str = f"Every {interval} days"

        notes = []
        if tz_warning:
            notes.append(tz_warning)
        if extra_ignored:
            notes.append("ℹ️ Additional roles (role2/role3) are ignored on the free plan — upgrade to Premium to ping multiple roles.")

        log.info(f"Event created: '{title}' (ID {event_id}) in guild {guild_id} by {ctx.author}")

        await ctx.followup.send(
            embed=build_success_embed(
                f"**{title}** posted in {channel.mention}!\n"
                f"ID: `{event_id}`  •  {recur_str}"
                + ("".join(notes))
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
        # Use start of today (UTC midnight) so events happening later today are included
        now = datetime.now(timezone.utc)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_iso = start_of_day.isoformat()

        with get_connection() as conn:
            rows = conn.execute(
                "SELECT id, title, start_time, timezone, channel_id, is_recurring, recur_rule FROM events WHERE guild_id=? AND start_time >= ? AND title NOT LIKE '[CANCELLED]%' ORDER BY start_time ASC",
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

        premium = is_premium(ctx.guild.id)
        await ctx.send_modal(EventButtonsModal(event=dict(row), premium=premium))

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

        # Guard against cancelling an already-cancelled event
        if event["title"].startswith("[CANCELLED]"):
            await ctx.respond(
                embed=build_error_embed("This event has already been cancelled."),
                ephemeral=True,
            )
            return

        cancelled_title = f"[CANCELLED] {event['title']}"
        cancelled_desc  = (event.get("description") or "") + "\n\n*This event has been cancelled.*"
        with get_connection() as conn:
            conn.execute("UPDATE events SET title=?, description=? WHERE id=?", (cancelled_title, cancelled_desc, event_id))
            conn.commit()

        await ctx.respond(embed=build_success_embed(f"Event **{event['title']}** (ID: `{event_id}`) marked as cancelled."), ephemeral=True)
        from cogs.rsvp import refresh_event_embed
        await refresh_event_embed(event_id, ctx.guild, ctx.bot)

        # ── Modlog: event cancelled ───────────────────────────────────────
        try:
            from cogs.modlogs import log_event, embed_event_cancelled
            cancelled_event = {**event, "title": cancelled_title}
            ml_embed = embed_event_cancelled(cancelled_event, ctx.author)
            await log_event(ctx.bot, ctx.guild.id, ml_embed)
        except Exception as e:
            log.warning(f"modlog hook failed (event cancelled): {e}")

    # ── /myevents ─────────────────────────────────────────────────────────
    @discord.slash_command(name="myevents", description="List events you've RSVPed to in this server.")
    async def myevents(self, ctx: discord.ApplicationContext):
        now_iso = datetime.now(timezone.utc).isoformat()
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT e.id, e.title, e.start_time, e.timezone, e.channel_id, r.status
                FROM rsvps r JOIN events e ON r.event_id = e.id
                WHERE r.user_id=? AND e.guild_id=? AND e.start_time >= ? AND r.status IN ('accepted','tentative')
                AND e.title NOT LIKE '[CANCELLED]%'
                ORDER BY e.start_time ASC
                """,
                (ctx.author.id, ctx.guild.id, now_iso),
            ).fetchall()
        if not rows:
            await ctx.respond(embed=build_error_embed("You haven't RSVPed to any upcoming events in this server."), ephemeral=True)
            return
        # Use guild custom color — consistent with /listevents
        cfg   = get_guild_config(ctx.guild.id)
        color = get_guild_color(cfg.get("embed_color") if cfg else None)
        embed = discord.Embed(title="📅  My RSVPs", color=color)
        for row in rows:
            try:
                tz = pytz.timezone(row["timezone"] or "UTC")
                dt = datetime.fromisoformat(row["start_time"]).astimezone(tz)
                ts = dt.strftime("%b %d, %Y  %I:%M %p %Z")
            except Exception:
                ts = row["start_time"]
            status_emoji = "✅" if row["status"] == "accepted" else "❓"
            ch = ctx.guild.get_channel(row["channel_id"])
            embed.add_field(name=f"{status_emoji} {row['title']}", value=f"🕐 {ts}  •  {ch.mention if ch else 'unknown channel'}", inline=False)
        await ctx.respond(embed=embed, ephemeral=True)

    # ── /exportevents ─────────────────────────────────────────────────────
    @discord.slash_command(name="exportevents", description="Export upcoming events as an .ics calendar file.")
    async def exportevents(self, ctx: discord.ApplicationContext):
        """Generates a standard iCalendar (.ics) file for all upcoming events."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE guild_id=? AND start_time >= ? ORDER BY start_time ASC",
                (ctx.guild.id, now_iso),
            ).fetchall()

        if not rows:
            await ctx.respond(embed=build_error_embed("No upcoming events to export."), ephemeral=True)
            return

        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            f"PRODID:-//Soren//Discord Events//EN",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
        ]

        for row in rows:
            event = dict(row)
            try:
                tz   = pytz.timezone(event.get("timezone") or "UTC")
                s_dt = datetime.fromisoformat(event["start_time"]).astimezone(pytz.utc)
                dtstart = s_dt.strftime("%Y%m%dT%H%M%SZ")
                if event.get("end_time"):
                    e_dt   = datetime.fromisoformat(event["end_time"]).astimezone(pytz.utc)
                    dtend  = e_dt.strftime("%Y%m%dT%H%M%SZ")
                else:
                    dtend  = (s_dt + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")
            except Exception:
                continue

            uid     = f"soren-event-{event['id']}@{ctx.guild.id}"
            summary = event["title"].replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            desc    = (event.get("description") or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

            lines += [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"SUMMARY:{summary}",
                f"DTSTART:{dtstart}",
                f"DTEND:{dtend}",
                f"DESCRIPTION:{desc}",
                "END:VEVENT",
            ]

        lines.append("END:VCALENDAR")
        ics_content = "\r\n".join(lines)
        file = discord.File(io.BytesIO(ics_content.encode("utf-8")), filename=f"{ctx.guild.name}_events.ics")

        await ctx.respond(
            embed=build_success_embed(f"Exported **{len(rows)}** upcoming event(s). Import the attached file into any calendar app."),
            file=file,
            ephemeral=True,
        )

    # ── /editeventmentions ────────────────────────────────────────────────
    @discord.slash_command(name="editeventmentions", description="Update or clear the mention/reminder roles for an event.")
    async def editeventmentions(
        self,
        ctx: discord.ApplicationContext,
        event_id: discord.Option(int, description="The event ID to update.", autocomplete=autocomplete_event_ids, required=True),
    ):
        if not is_event_creator(ctx.author):
            await ctx.respond(embed=build_error_embed("You don't have the Event Creator role."), ephemeral=True)
            return
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM events WHERE id=? AND guild_id=?", (event_id, ctx.guild.id)).fetchone()
        if not row:
            await ctx.respond(embed=build_error_embed(f"No event found with ID `{event_id}`."), ephemeral=True)
            return
        premium = is_premium(ctx.guild.id)
        await ctx.send_modal(EditEventMentionsModal(event=dict(row), guild=ctx.guild, premium=premium))


def setup(bot: discord.Bot):
    bot.add_cog(Events(bot))