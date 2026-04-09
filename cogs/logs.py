"""
cogs/logs.py — Bot Owner Log Management
=========================================
Slash commands for viewing, exporting, and managing Soren's daily log files.
All commands are restricted to the bot owner (BOT_OWNER_ID in .env).

Commands:
  /logs list              — List all log files with size and date
  /logs view <date>       — Show last N lines from a date's log (default 50)
  /logs export <date>     — Send the log file as a .txt attachment
  /logs delete <date>     — Delete a specific log file (with confirmation)
  /logs clear             — Delete ALL log files (with confirmation)
  /logs errors <date>     — Show only ERROR and WARNING lines from a date's log

Date format: YYYY-MM-DD  (e.g. 2026-04-09)
"""

import discord
from discord.ext import commands
import os
import logging
from datetime import datetime

log = logging.getLogger("soren.logs")

# ── Constants ──────────────────────────────────────────────────────────────────
LOG_DIR          = os.path.join(os.path.dirname(__file__), "..", "logs")
DATE_FMT         = "%Y-%m-%d"          # What the user types
FILE_DATE_FMT    = "%Y_%m_%d"          # What's in the filename
DEFAULT_LINES    = 50                  # Lines shown by /logs view
MAX_LINES        = 200                 # Hard cap to avoid embed overflow
MAX_CHARS        = 3800                # Stay safely under Discord's 4096 embed limit


# ── Helpers ────────────────────────────────────────────────────────────────────

def _log_path(date_str: str) -> str:
    """Convert a YYYY-MM-DD string to the full path of that day's log file."""
    try:
        dt = datetime.strptime(date_str, DATE_FMT)
        filename = f"soren_{dt.strftime(FILE_DATE_FMT)}.log"
        return os.path.join(LOG_DIR, filename)
    except ValueError:
        return None


def _all_log_files() -> list[dict]:
    """
    Return a sorted list of all log files in LOG_DIR.
    Each entry: { 'filename': str, 'path': str, 'size_kb': float, 'date': str }
    """
    if not os.path.isdir(LOG_DIR):
        return []

    files = []
    for name in sorted(os.listdir(LOG_DIR)):
        if not name.startswith("soren_") or not name.endswith(".log"):
            continue
        path = os.path.join(LOG_DIR, name)
        size_kb = os.path.getsize(path) / 1024
        # Parse date from filename: soren_YYYY_MM_DD.log
        date_part = name[len("soren_"):-len(".log")]  # e.g. "2026_04_09"
        try:
            dt = datetime.strptime(date_part, FILE_DATE_FMT)
            date_display = dt.strftime("%B %d, %Y")   # "April 09, 2026"
            date_key     = dt.strftime(DATE_FMT)       # "2026-04-09"
        except ValueError:
            date_display = date_part
            date_key     = date_part
        files.append({
            "filename":     name,
            "path":         path,
            "size_kb":      size_kb,
            "date_display": date_display,
            "date_key":     date_key,
        })
    return files


def _tail(path: str, n: int) -> list[str]:
    """Return the last n lines of a file."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return lines[-n:]


def _filter_errors(path: str) -> list[str]:
    """Return only ERROR and WARNING lines from a file."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return [l for l in f if " [ERROR] " in l or " [WARNING] " in l]


def _truncate_for_embed(lines: list[str], max_chars: int = MAX_CHARS) -> str:
    """
    Join lines into a string that fits inside a Discord embed code block.
    If truncated, prepends a notice.
    """
    joined = "".join(lines)
    if len(joined) <= max_chars:
        return joined
    # Take from the end (most recent) and note truncation
    truncated = joined[-max_chars:]
    # Trim to a clean line boundary
    first_newline = truncated.find("\n")
    if first_newline != -1:
        truncated = truncated[first_newline + 1:]
    return f"[… truncated — showing most recent lines …]\n{truncated}"


# ── Owner check ────────────────────────────────────────────────────────────────

def _is_owner(ctx: discord.ApplicationContext) -> bool:
    owner_id_str = os.getenv("BOT_OWNER_ID", "")
    if not owner_id_str:
        return False
    try:
        return ctx.author.id == int(owner_id_str)
    except ValueError:
        return False


async def _owner_only(ctx: discord.ApplicationContext) -> bool:
    """Respond with an error and return False if caller is not the bot owner."""
    if _is_owner(ctx):
        return True
    await ctx.respond(
        "🔒 This command is restricted to the bot owner.",
        ephemeral=True
    )
    return False


# ── Confirmation view ──────────────────────────────────────────────────────────

class ConfirmView(discord.ui.View):
    """Generic two-button confirm/cancel view used for destructive actions."""

    def __init__(self, on_confirm, label: str = "Confirm", timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self._on_confirm = on_confirm
        self._confirm_btn = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.danger,
            custom_id="confirm",
        )
        self._cancel_btn = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id="cancel",
        )
        self._confirm_btn.callback = self._confirm
        self._cancel_btn.callback = self._cancel
        self.add_item(self._confirm_btn)
        self.add_item(self._cancel_btn)

    async def _confirm(self, interaction: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await self._on_confirm(interaction)
        self.stop()

    async def _cancel(self, interaction: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="❌ Cancelled.", embed=None, view=self
        )
        self.stop()

    async def on_timeout(self):
        # Disable buttons silently on timeout — no message edit needed
        self.stop()


# ── Cog ────────────────────────────────────────────────────────────────────────

class Logs(commands.Cog):
    """Bot owner commands for managing Soren's log files."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    logs = discord.SlashCommandGroup(
        "logs",
        "Bot owner — manage Soren's log files",
    )

    # ── /logs list ─────────────────────────────────────────────────────────────

    @logs.command(name="list", description="List all log files with size and date.")
    async def logs_list(self, ctx: discord.ApplicationContext):
        if not await _owner_only(ctx):
            return

        files = _all_log_files()

        if not files:
            await ctx.respond("📂 No log files found.", ephemeral=True)
            return

        lines = []
        total_kb = 0.0
        for f in files:
            size_str = f"{f['size_kb']:.1f} KB"
            lines.append(f"`{f['date_key']}`  —  {f['date_display']}  ({size_str})")
            total_kb += f["size_kb"]

        embed = discord.Embed(
            title="📋 Soren Log Files",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{len(files)} file(s)  •  {total_kb:.1f} KB total")

        await ctx.respond(embed=embed, ephemeral=True)

    # ── /logs view ─────────────────────────────────────────────────────────────

    @logs.command(name="view", description="Show the last N lines from a date's log.")
    async def logs_view(
        self,
        ctx: discord.ApplicationContext,
        date: discord.Option(str, "Date to view (YYYY-MM-DD)", required=True),
        lines: discord.Option(int, f"Number of lines (default {DEFAULT_LINES}, max {MAX_LINES})", required=False, default=DEFAULT_LINES),
    ):
        if not await _owner_only(ctx):
            return

        path = _log_path(date)
        if path is None:
            await ctx.respond("⚠️ Invalid date format. Use `YYYY-MM-DD`.", ephemeral=True)
            return
        if not os.path.isfile(path):
            await ctx.respond(f"📂 No log file found for `{date}`.", ephemeral=True)
            return

        n = max(1, min(lines, MAX_LINES))
        tail_lines = _tail(path, n)

        if not tail_lines:
            await ctx.respond(f"📄 Log file for `{date}` is empty.", ephemeral=True)
            return

        content = _truncate_for_embed(tail_lines)

        embed = discord.Embed(
            title=f"📄 Log — {date}  (last {len(tail_lines)} lines)",
            description=f"```\n{content}\n```",
            color=discord.Color.blurple(),
        )
        file_size = os.path.getsize(path) / 1024
        embed.set_footer(text=f"File size: {file_size:.1f} KB  •  Use /logs export to get the full file")

        await ctx.respond(embed=embed, ephemeral=True)

    # ── /logs export ───────────────────────────────────────────────────────────

    @logs.command(name="export", description="Send a log file as a .txt attachment.")
    async def logs_export(
        self,
        ctx: discord.ApplicationContext,
        date: discord.Option(str, "Date to export (YYYY-MM-DD)", required=True),
    ):
        if not await _owner_only(ctx):
            return

        path = _log_path(date)
        if path is None:
            await ctx.respond("⚠️ Invalid date format. Use `YYYY-MM-DD`.", ephemeral=True)
            return
        if not os.path.isfile(path):
            await ctx.respond(f"📂 No log file found for `{date}`.", ephemeral=True)
            return

        file_size_kb = os.path.getsize(path) / 1024
        discord_file = discord.File(path, filename=f"soren_{date}.txt")

        await ctx.respond(
            content=f"📎 Log export for `{date}` ({file_size_kb:.1f} KB)",
            file=discord_file,
            ephemeral=True,
        )
        log.info(f"Log exported by owner: {date}")

    # ── /logs delete ───────────────────────────────────────────────────────────

    @logs.command(name="delete", description="Delete a specific log file.")
    async def logs_delete(
        self,
        ctx: discord.ApplicationContext,
        date: discord.Option(str, "Date to delete (YYYY-MM-DD)", required=True),
    ):
        if not await _owner_only(ctx):
            return

        path = _log_path(date)
        if path is None:
            await ctx.respond("⚠️ Invalid date format. Use `YYYY-MM-DD`.", ephemeral=True)
            return
        if not os.path.isfile(path):
            await ctx.respond(f"📂 No log file found for `{date}`.", ephemeral=True)
            return

        file_size_kb = os.path.getsize(path) / 1024

        async def do_delete(interaction: discord.Interaction):
            try:
                os.remove(path)
                log.info(f"Log file deleted by owner: soren_{date}.log")
                await interaction.followup.send(
                    f"🗑️ Deleted log file for `{date}` ({file_size_kb:.1f} KB).",
                    ephemeral=True,
                )
            except OSError as e:
                log.error(f"Failed to delete log file {path}: {e}")
                await interaction.followup.send(
                    f"❌ Failed to delete file: `{e}`", ephemeral=True
                )

        embed = discord.Embed(
            title="🗑️ Delete Log File?",
            description=f"This will permanently delete the log for **{date}** ({file_size_kb:.1f} KB).\n\nThis cannot be undone.",
            color=discord.Color.red(),
        )
        view = ConfirmView(on_confirm=do_delete, label="Delete")
        await ctx.respond(embed=embed, view=view, ephemeral=True)

    # ── /logs clear ────────────────────────────────────────────────────────────

    @logs.command(name="clear", description="Delete ALL log files. Cannot be undone.")
    async def logs_clear(self, ctx: discord.ApplicationContext):
        if not await _owner_only(ctx):
            return

        files = _all_log_files()
        if not files:
            await ctx.respond("📂 No log files to delete.", ephemeral=True)
            return

        total_kb = sum(f["size_kb"] for f in files)

        async def do_clear(interaction: discord.Interaction):
            deleted = 0
            failed  = 0
            for f in files:
                try:
                    os.remove(f["path"])
                    deleted += 1
                except OSError as e:
                    log.error(f"Failed to delete {f['filename']}: {e}")
                    failed += 1
            log.info(f"Log clear executed by owner: {deleted} deleted, {failed} failed")
            msg = f"🗑️ Deleted **{deleted}** log file(s)"
            if failed:
                msg += f" ({failed} could not be deleted — check server logs)"
            await interaction.followup.send(msg, ephemeral=True)

        embed = discord.Embed(
            title="⚠️ Clear All Logs?",
            description=(
                f"This will permanently delete **{len(files)} log file(s)** "
                f"({total_kb:.1f} KB total).\n\n"
                "**This cannot be undone.**"
            ),
            color=discord.Color.red(),
        )
        view = ConfirmView(on_confirm=do_clear, label="Clear All")
        await ctx.respond(embed=embed, view=view, ephemeral=True)

    # ── /logs errors ───────────────────────────────────────────────────────────

    @logs.command(name="errors", description="Show only ERROR and WARNING lines from a date's log.")
    async def logs_errors(
        self,
        ctx: discord.ApplicationContext,
        date: discord.Option(str, "Date to check (YYYY-MM-DD)", required=True),
    ):
        if not await _owner_only(ctx):
            return

        path = _log_path(date)
        if path is None:
            await ctx.respond("⚠️ Invalid date format. Use `YYYY-MM-DD`.", ephemeral=True)
            return
        if not os.path.isfile(path):
            await ctx.respond(f"📂 No log file found for `{date}`.", ephemeral=True)
            return

        error_lines = _filter_errors(path)

        if not error_lines:
            await ctx.respond(
                f"✅ No ERROR or WARNING lines found in the log for `{date}`.",
                ephemeral=True,
            )
            return

        content = _truncate_for_embed(error_lines)

        embed = discord.Embed(
            title=f"⚠️ Errors & Warnings — {date}  ({len(error_lines)} line(s))",
            description=f"```\n{content}\n```",
            color=discord.Color.orange(),
        )
        embed.set_footer(text="Use /logs export to get the full file")

        await ctx.respond(embed=embed, ephemeral=True)


# ── Setup ──────────────────────────────────────────────────────────────────────

def setup(bot: discord.Bot):
    bot.add_cog(Logs(bot))
    log.info("Logs cog loaded.")