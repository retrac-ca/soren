"""
cogs/rsvp.py
=============
Handles all button interactions on event embeds.

Button layout on each event post
----------------------------------
  [ <accept_label> ]  [ <tentative_label> (optional) ]  [ <decline_label> ]

  Up to 2 additional custom buttons can be added via /eventbuttons.
  Edit buttons have been removed — use /editeventdetails and /editeventtime.

RSVP buttons — available to everyone:
  Clicking a button upserts the user's RSVP row and refreshes the embed.
  A per-user cooldown prevents button spam.

Free tier caps the displayed RSVP list at 50 names per status column.
"""

import discord
from discord.ext import commands

from utils.database import get_connection, is_premium
from utils.embeds import build_event_embed
from datetime import datetime, timezone
import logging

log = logging.getLogger("soren.rsvp")

# ── Free tier display cap ─────────────────────────────────────────────────────
FREE_RSVP_DISPLAY_LIMIT = 50

# ── Button label defaults ─────────────────────────────────────────────────────
DEFAULT_ACCEPT_LABEL    = "✅ Accept"
DEFAULT_TENTATIVE_LABEL = "❓ Tentative"
DEFAULT_DECLINE_LABEL   = "❌ Decline"

# ── RSVP cooldown (per user per event, in seconds) ────────────────────────────
RSVP_COOLDOWN_SECONDS = 3
_rsvp_cooldowns: dict[tuple, datetime] = {}


# ── Database helpers ──────────────────────────────────────────────────────────

def fetch_event(event_id: int) -> dict | None:
    """Return a single event row as a dict, or None if not found."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    return dict(row) if row else None


def fetch_rsvps_for_embed(event_id: int, guild: discord.Guild, premium: bool) -> dict:
    """
    Build the rsvps dict expected by build_event_embed().
    Resolves user IDs to display names and applies the free-tier cap.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT user_id, status FROM rsvps WHERE event_id=? ORDER BY updated_at ASC",
            (event_id,),
        ).fetchall()

    result = {"accepted": [], "declined": [], "tentative": []}
    counts = {"accepted": 0, "declined": 0, "tentative": 0}

    for row in rows:
        status  = row["status"]
        user_id = row["user_id"]
        counts[status] += 1

        limit = None if premium else FREE_RSVP_DISPLAY_LIMIT
        if limit and len(result[status]) >= limit:
            continue

        member = guild.get_member(user_id)
        name   = member.display_name if member else f"<User {user_id}>"
        result[status].append(name)

    if not premium:
        for status in result:
            overflow = counts[status] - len(result[status])
            if overflow > 0:
                result[status].append(f"*(+{overflow} more — upgrade to Premium to see all)*")

    return result


async def _promote_from_waitlist(event_id: int, guild: discord.Guild, bot: discord.Bot):
    """
    When an accepted RSVP is removed, check the waitlist and notify
    the next person in line that a spot has opened up.
    """
    with get_connection() as conn:
        # Check if table exists first (graceful degradation)
        try:
            next_entry = conn.execute(
                "SELECT * FROM waitlist WHERE event_id=? ORDER BY id ASC LIMIT 1",
                (event_id,),
            ).fetchone()
        except Exception:
            return

    if not next_entry:
        return

    member = guild.get_member(next_entry["user_id"])
    if not member:
        return

    with get_connection() as conn:
        event_row = conn.execute("SELECT title FROM events WHERE id=?", (event_id,)).fetchone()
    if not event_row:
        return

    try:
        await member.send(
            f"🎉 A spot has opened up for **{event_row['title']}**! "
            f"Head back to the event and click ✅ Accept to claim your spot."
        )
        log.info(f"Waitlist: notified {member} for event {event_id}")
    except discord.Forbidden:
        log.warning(f"Waitlist: could not DM {member} (DMs disabled)")


async def refresh_event_embed(event_id: int, guild: discord.Guild, bot: discord.Bot):
    """
    Re-fetch RSVP data and edit the original Discord message so the embed
    always shows the current attendee list.
    """
    event = fetch_event(event_id)
    if not event:
        return

    from utils.database import get_guild_config
    cfg   = get_guild_config(guild.id)
    event = {**event, "embed_color": cfg.get("embed_color") if cfg else None}

    premium = is_premium(guild.id)
    rsvps   = fetch_rsvps_for_embed(event_id, guild, premium)
    embed   = build_event_embed(event, rsvps)

    try:
        channel = guild.get_channel(event["channel_id"])
        if not channel:
            log.warning(f"refresh_event_embed: channel {event['channel_id']} not found for event {event_id}")
            return
        if event.get("message_id"):
            msg  = await channel.fetch_message(event["message_id"])
            view = EventView(event_id=event_id, event=event)
            await msg.edit(embed=embed, view=view)
    except discord.NotFound:
        log.warning(f"refresh_event_embed: message not found for event {event_id}")
    except discord.Forbidden:
        log.warning(f"refresh_event_embed: no permission to edit message for event {event_id} in channel {event['channel_id']}")
    except Exception as e:
        log.error(f"refresh_event_embed: unexpected error for event {event_id}: {e}")


# ── Dynamic event view ────────────────────────────────────────────────────────

class EventView(discord.ui.View):
    """
    Persistent view with up to 5 RSVP buttons.
    Edit buttons have been removed — editing is done via slash commands.
    timeout=None keeps buttons active indefinitely across bot restarts.
    """

    def __init__(self, event_id: int, event: dict | None = None):
        super().__init__(timeout=None)
        self.event_id = event_id

        accept_label    = DEFAULT_ACCEPT_LABEL
        tentative_label = DEFAULT_TENTATIVE_LABEL
        decline_label   = DEFAULT_DECLINE_LABEL
        show_tentative  = True

        if event:
            accept_label    = event.get("btn_accept_label")    or DEFAULT_ACCEPT_LABEL
            tentative_label = event.get("btn_tentative_label") or DEFAULT_TENTATIVE_LABEL
            decline_label   = event.get("btn_decline_label")   or DEFAULT_DECLINE_LABEL
            show_tentative  = bool(event.get("btn_tentative_enabled", 1))

        # ── Accept button ─────────────────────────────────────────────────
        accept_btn = discord.ui.Button(
            label=accept_label,
            style=discord.ButtonStyle.success,
            custom_id=f"rsvp_accept:{event_id}",
        )
        accept_btn.callback = self._make_rsvp_callback("accepted")
        self.add_item(accept_btn)

        # ── Tentative button (optional) ───────────────────────────────────
        if show_tentative:
            tentative_btn = discord.ui.Button(
                label=tentative_label,
                style=discord.ButtonStyle.secondary,
                custom_id=f"rsvp_tentative:{event_id}",
            )
            tentative_btn.callback = self._make_rsvp_callback("tentative")
            self.add_item(tentative_btn)

        # ── Decline button ────────────────────────────────────────────────
        decline_btn = discord.ui.Button(
            label=decline_label,
            style=discord.ButtonStyle.danger,
            custom_id=f"rsvp_decline:{event_id}",
        )
        decline_btn.callback = self._make_rsvp_callback("declined")
        self.add_item(decline_btn)

    def _make_rsvp_callback(self, status: str):
        """Returns an async callback bound to the given RSVP status."""
        async def callback(interaction: discord.Interaction):
            # ── Cooldown check ────────────────────────────────────────────
            cooldown_key = (interaction.user.id, self.event_id)
            now = datetime.now(timezone.utc)
            last = _rsvp_cooldowns.get(cooldown_key)
            if last and (now - last).total_seconds() < RSVP_COOLDOWN_SECONDS:
                await interaction.response.send_message(
                    "You're clicking too fast — please wait a moment before changing your RSVP.",
                    ephemeral=True,
                )
                return
            _rsvp_cooldowns[cooldown_key] = now

            event = fetch_event(self.event_id)
            if not event:
                await interaction.response.send_message("This event no longer exists.", ephemeral=True)
                return

            # ── Max RSVP / waitlist check ─────────────────────────────────
            if status == "accepted" and event.get("max_rsvp", 0) > 0:
                with get_connection() as conn:
                    count = conn.execute(
                        "SELECT COUNT(*) as cnt FROM rsvps WHERE event_id=? AND status='accepted'",
                        (self.event_id,),
                    ).fetchone()["cnt"]
                if count >= event["max_rsvp"]:
                    from cogs.events import WaitlistView
                    await interaction.response.send_message(
                        "This event is full! Would you like to join the waitlist?",
                        view=WaitlistView(event_id=self.event_id),
                        ephemeral=True,
                    )
                    return

            # ── Check previous RSVP status ────────────────────────────────
            was_accepted = False
            already_this_status = False
            with get_connection() as conn:
                prev = conn.execute(
                    "SELECT status FROM rsvps WHERE event_id=? AND user_id=?",
                    (self.event_id, interaction.user.id),
                ).fetchone()
                if prev:
                    if prev["status"] == status:
                        already_this_status = True
                    elif prev["status"] == "accepted" and status != "accepted":
                        was_accepted = True

            # ── Toggle off if same button clicked again ────────────────────
            if already_this_status:
                with get_connection() as conn:
                    conn.execute(
                        "DELETE FROM rsvps WHERE event_id=? AND user_id=?",
                        (self.event_id, interaction.user.id),
                    )
                    conn.commit()
                log.info(f"RSVP: {interaction.user} removed RSVP from event {self.event_id}")
                await interaction.response.send_message(
                    f"Your RSVP for **{event['title']}** has been removed.",
                    ephemeral=True,
                )
                await refresh_event_embed(self.event_id, interaction.guild, interaction.client)

                # ── Modlog: RSVP removed ──────────────────────────────────
                try:
                    from cogs.modlogs import log_event, embed_rsvp
                    ml_embed = embed_rsvp(event["title"], self.event_id, interaction.user, "removed")
                    await log_event(interaction.client, interaction.guild_id, ml_embed)
                except Exception:
                    pass
                return

            # ── Upsert RSVP ───────────────────────────────────────────────
            with get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO rsvps (event_id, user_id, status, updated_at)
                    VALUES (?, ?, ?, datetime('now'))
                    ON CONFLICT(event_id, user_id) DO UPDATE
                    SET status=excluded.status, updated_at=excluded.updated_at
                    """,
                    (self.event_id, interaction.user.id, status),
                )
                conn.commit()

            log.info(f"RSVP: {interaction.user} set status='{status}' on event {self.event_id} in guild {interaction.guild_id}")

            label_map = {
                "accepted":  event.get("btn_accept_label")    or DEFAULT_ACCEPT_LABEL,
                "declined":  event.get("btn_decline_label")   or DEFAULT_DECLINE_LABEL,
                "tentative": event.get("btn_tentative_label") or DEFAULT_TENTATIVE_LABEL,
            }
            await interaction.response.send_message(
                f"Your RSVP for **{event['title']}** has been set to **{label_map[status]}**.",
                ephemeral=True,
            )

            await refresh_event_embed(self.event_id, interaction.guild, interaction.client)

            # ── Modlog: RSVP update ───────────────────────────────────────
            try:
                from cogs.modlogs import log_event, embed_rsvp
                ml_embed = embed_rsvp(event["title"], self.event_id, interaction.user, status)
                await log_event(interaction.client, interaction.guild_id, ml_embed)
            except Exception as e:
                pass  # Non-critical — don't log modlog failures for every RSVP click

            # ── Waitlist promotion ────────────────────────────────────────
            if was_accepted:
                await _promote_from_waitlist(self.event_id, interaction.guild, interaction.client)

        return callback


# ── Cog ───────────────────────────────────────────────────────────────────────

class RSVP(commands.Cog):
    """Registers the persistent EventView on bot startup."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        """Re-register persistent views on every bot start."""
        self.bot.add_view(EventView(event_id=0))

        now_iso = datetime.now(timezone.utc).isoformat()
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE message_id IS NOT NULL AND start_time > ?",
                (now_iso,),
            ).fetchall()
        for row in rows:
            event = dict(row)
            self.bot.add_view(EventView(event_id=event["id"], event=event))


def setup(bot: discord.Bot):
    bot.add_cog(RSVP(bot))