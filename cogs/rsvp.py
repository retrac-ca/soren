"""
cogs/rsvp.py
=============
Handles all button interactions on event embeds.

Button layout on each event post
----------------------------------
  [ ✅ Accept ]  [ ❓ Tentative ]  [ ❌ Decline ]  [ ✏️ Edit ]

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


# ── Free tier display cap ─────────────────────────────────────────────────────
FREE_RSVP_DISPLAY_LIMIT = 50


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

        # Skip rendering if we've hit the free tier cap
        limit = None if premium else FREE_RSVP_DISPLAY_LIMIT
        if limit and len(result[status]) >= limit:
            continue

        member = guild.get_member(user_id)
        name   = member.display_name if member else f"<User {user_id}>"
        result[status].append(name)

    # Show overflow notice for free tier
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

    premium = is_premium(guild.id)
    rsvps   = fetch_rsvps_for_embed(event_id, guild, premium)
    embed   = build_event_embed(event, rsvps)

    try:
        channel = guild.get_channel(event["channel_id"])
        if channel and event.get("message_id"):
            msg  = await channel.fetch_message(event["message_id"])
            view = EventView(event_id=event_id)
            await msg.edit(embed=embed, view=view)
    except (discord.NotFound, discord.Forbidden):
        pass   # Message gone or no permission — not critical


# ── Main button view attached to every event embed ────────────────────────────
class EventView(discord.ui.View):
    """
    Persistent view with four buttons:
      ✅ Accept  |  ❓ Tentative  |  ❌ Decline  |  ✏️ Edit

    timeout=None keeps buttons active indefinitely (across bot restarts
    once the view is re-registered in on_ready).
    """

    def __init__(self, event_id: int):
        super().__init__(timeout=None)
        self.event_id = event_id
        # Encode event_id into each button's custom_id so interactions
        # still work correctly after a bot restart.
        for child in self.children:
            child.custom_id = f"{child.custom_id}:{event_id}"

    # ── RSVP buttons ─────────────────────────────────────────────────────

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success,
                       custom_id="rsvp_accept")
    async def accept(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._handle_rsvp(interaction, "accepted")

    @discord.ui.button(label="❓ Tentative", style=discord.ButtonStyle.secondary,
                       custom_id="rsvp_tentative")
    async def tentative(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._handle_rsvp(interaction, "tentative")

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger,
                       custom_id="rsvp_decline")
    async def decline(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._handle_rsvp(interaction, "declined")

    # ── Edit button ──────────────────────────────────────────────────────

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.primary,
                       custom_id="event_edit")
    async def edit_event(self, button: discord.ui.Button, interaction: discord.Interaction):
        """
        Opens the EditEventModal pre-filled with this event's current data.
        Only available to members with the Event Creator role or server admins.
        """
        # Permission check — only event creators / admins can edit
        if not is_event_creator(interaction.user):
            await interaction.response.send_message(
                "❌ You need the **Event Creator** role to edit events.",
                ephemeral=True,
            )
            return

        # Fetch the latest event data to pre-fill the modal
        event = fetch_event(self.event_id)
        if not event:
            await interaction.response.send_message(
                "This event no longer exists.", ephemeral=True
            )
            return

        # Import here to avoid circular import at module level
        from cogs.events import EditEventModal
        await interaction.response.send_modal(EditEventModal(event=event))

    # ── Shared RSVP handler ──────────────────────────────────────────────

    async def _handle_rsvp(self, interaction: discord.Interaction, status: str):
        """Upsert the user's RSVP and refresh the embed."""
        event = fetch_event(self.event_id)
        if not event:
            await interaction.response.send_message(
                "This event no longer exists.", ephemeral=True
            )
            return

        # Upsert: insert or update this user's RSVP row
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

        # Respond immediately (Discord requires a response within 3 seconds)
        label_map = {
            "accepted":  "✅ Accepted",
            "declined":  "❌ Declined",
            "tentative": "❓ Tentative",
        }
        await interaction.response.send_message(
            f"Your RSVP for **{event['title']}** has been set to **{label_map[status]}**.",
            ephemeral=True,
        )

        # Refresh the public embed
        await refresh_event_embed(self.event_id, interaction.guild, interaction.client)


# ── Cog ───────────────────────────────────────────────────────────────────────
class RSVP(commands.Cog):
    """Registers the persistent EventView on bot startup."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        """
        Re-register the persistent view every time the bot starts.
        Without this, the buttons stop responding after a restart.
        We register with event_id=0 — the custom_id prefix is what matters.
        """
        self.bot.add_view(EventView(event_id=0))


def setup(bot: discord.Bot):
    bot.add_cog(RSVP(bot))
