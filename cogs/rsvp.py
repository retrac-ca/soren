"""
cogs/rsvp.py
=============
Handles all button interactions on event embeds.

Button layout on each event post
----------------------------------
  [ <accept_label> ]  [ <tentative_label> ]  [ <decline_label> ]  [ ✏️ Edit ]

  Tentative button is optional — hidden if btn_tentative_enabled=0.
  Button labels are fully customizable per event.

RSVP buttons — available to everyone:
  Clicking a button upserts the user's RSVP row and refreshes the embed.

Edit button — visible to everyone, but restricted by role check:
  Only members with the Event Creator role (or server admins) can use it.
  Clicking it opens the EditEventModal pre-filled with the current event data.

Free tier caps the displayed RSVP list at 50 names per status column.
"""

import discord
from discord.ext import commands

from utils.database import get_connection, is_premium
from utils.permissions import is_event_creator
from utils.embeds import build_event_embed
import logging

log = logging.getLogger("soren.rsvp")


# ── Free tier display cap ─────────────────────────────────────────────────────
FREE_RSVP_DISPLAY_LIMIT = 50

# ── Button label defaults ─────────────────────────────────────────────────────
DEFAULT_ACCEPT_LABEL    = "✅ Accept"
DEFAULT_TENTATIVE_LABEL = "❓ Tentative"
DEFAULT_DECLINE_LABEL   = "❌ Decline"


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
                result[status].append(
                    f"*(+{overflow} more — upgrade to Premium to see all)*"
                )

    return result


async def refresh_event_embed(event_id: int, guild: discord.Guild, bot: discord.Bot):
    """
    Re-fetch RSVP data and edit the original Discord message so the embed
    always shows the current attendee list.
    Called after any RSVP change or event edit.
    """
    event = fetch_event(event_id)
    if not event:
        return

    # Attach guild embed color
    from utils.database import get_guild_config
    cfg = get_guild_config(guild.id)
    event = {**event, "embed_color": cfg.get("embed_color") if cfg else None}

    premium = is_premium(guild.id)
    rsvps   = fetch_rsvps_for_embed(event_id, guild, premium)
    embed   = build_event_embed(event, rsvps)

    try:
        channel = guild.get_channel(event["channel_id"])
        if channel and event.get("message_id"):
            msg  = await channel.fetch_message(event["message_id"])
            view = EventView(event_id=event_id, event=event)
            await msg.edit(embed=embed, view=view)
    except discord.NotFound:
        log.warning(f"refresh_event_embed: message not found for event {event_id}")
    except discord.Forbidden:
        log.warning(f"refresh_event_embed: no permission to edit message for event {event_id}")
    except Exception as e:
        log.error(f"refresh_event_embed: unexpected error for event {event_id}: {e}")


# ── Dynamic event view ────────────────────────────────────────────────────────
class EventView(discord.ui.View):
    """
    Persistent view with up to four buttons:
      <accept>  |  <tentative> (optional)  |  <decline>  |  ✏️ Edit

    Button labels and tentative visibility are read from the event record.
    timeout=None keeps buttons active indefinitely across bot restarts.
    """

    def __init__(self, event_id: int, event: dict | None = None):
        super().__init__(timeout=None)
        self.event_id = event_id

        # Read config from event dict (fall back to defaults if not provided)
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

        # ── Edit button (always shown, role-restricted) ───────────────────
        edit_btn = discord.ui.Button(
            label="✏️ Edit",
            style=discord.ButtonStyle.primary,
            custom_id=f"event_edit:{event_id}",
        )
        edit_btn.callback = self._edit_callback
        self.add_item(edit_btn)

    def _make_rsvp_callback(self, status: str):
        """Returns an async callback bound to the given RSVP status."""
        async def callback(interaction: discord.Interaction):
            event = fetch_event(self.event_id)
            if not event:
                await interaction.response.send_message(
                    "This event no longer exists.", ephemeral=True
                )
                return

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

            log.info(
                f"RSVP: {interaction.user} set status='{status}' "
                f"on event {self.event_id} in guild {interaction.guild_id}"
            )

            # Use the custom label for the confirmation message
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

        return callback

    async def _edit_callback(self, interaction: discord.Interaction):
        """Opens the EditEventModal. Restricted to Event Creator role / admins."""
        if not is_event_creator(interaction.user):
            await interaction.response.send_message(
                "❌ You need the **Event Creator** role to edit events.",
                ephemeral=True,
            )
            return

        event = fetch_event(self.event_id)
        if not event:
            await interaction.response.send_message(
                "This event no longer exists.", ephemeral=True
            )
            return

        from cogs.events import EditEventModal
        await interaction.response.send_modal(EditEventModal(event=event))


# ── Cog ───────────────────────────────────────────────────────────────────────
class RSVP(commands.Cog):
    """Registers the persistent EventView on bot startup."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        """
        Re-register the persistent view every time the bot starts.
        Without this, buttons stop responding after a restart.
        We register with event_id=0 and no event dict — the custom_id
        prefix is what Discord uses to route interactions.
        """
        self.bot.add_view(EventView(event_id=0))


def setup(bot: discord.Bot):
    bot.add_cog(RSVP(bot))