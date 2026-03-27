"""
utils/database.py
==================
All SQLite database setup and shared helper functions.
Every table used by Soren is defined and created here via init_db().
"""

import sqlite3
import os
import logging

log = logging.getLogger("soren.db")

# Path to the SQLite file — stored in the data/ folder
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "soren.db")


def get_connection() -> sqlite3.Connection:
    """
    Return a new SQLite connection with row_factory set so rows
    behave like dictionaries (access columns by name).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """
    Create all tables if they don't already exist.
    Safe to call on every startup — uses CREATE TABLE IF NOT EXISTS.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    conn = get_connection()
    cursor = conn.cursor()

    # ── Guild configuration ──────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id        INTEGER PRIMARY KEY,
            creator_role_id INTEGER,
            is_premium      INTEGER DEFAULT 0,
            gcal_token      TEXT,
            gcal_id         TEXT
        )
    """)

    # ── Discord-created events (slash command events with RSVP) ──────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id        INTEGER NOT NULL,
            channel_id      INTEGER NOT NULL,
            message_id      INTEGER,
            creator_id      INTEGER NOT NULL,
            title           TEXT    NOT NULL,
            description     TEXT,
            timezone        TEXT    DEFAULT 'UTC',
            start_time      TEXT    NOT NULL,
            end_time        TEXT,
            is_recurring    INTEGER DEFAULT 0,
            recur_rule      TEXT,
            recur_interval  INTEGER DEFAULT 1,
            recur_end       TEXT,
            parent_event_id INTEGER,
            reminder_offset INTEGER DEFAULT 15,
            notify_role_id  INTEGER,
            gcal_event_id   TEXT,
            created_at      TEXT    DEFAULT (datetime('now')),
            FOREIGN KEY (guild_id) REFERENCES guild_config(guild_id)
        )
    """)

    # ── RSVPs ────────────────────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rsvps (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            status      TEXT    NOT NULL CHECK(status IN ('accepted','declined','tentative')),
            updated_at  TEXT    DEFAULT (datetime('now')),
            UNIQUE(event_id, user_id),
            FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
        )
    """)

    # ── G-Cal Integrations ───────────────────────────────────────────────
    # Stores one row per connected Google Calendar per guild.
    # Each calendar is completely independent: its own OAuth token,
    # its own target Discord channel, and its own posting schedule.
    # This table is ONLY used by the gcal_integrations cog and has
    # no connection to the slash-command events table above.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS gcal_integrations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id        INTEGER NOT NULL,
            label           TEXT    NOT NULL,   -- Friendly name, e.g. "MMA Events"
            calendar_id     TEXT    NOT NULL,   -- Google Calendar ID (e.g. abc@group.calendar.google.com)
            gcal_token      TEXT    NOT NULL,   -- OAuth2 token JSON for this calendar
            channel_id      INTEGER NOT NULL,   -- Discord channel to post summaries into
            schedule        TEXT    DEFAULT 'weekly', -- 'daily', 'weekly', or 'custom'
            custom_interval INTEGER DEFAULT 7,  -- Days between posts (used when schedule='custom')
            post_day        TEXT    DEFAULT 'monday', -- Day of week to post (weekly schedule)
            post_hour       INTEGER DEFAULT 9,  -- Hour of day to post (0-23, server local time)
            last_posted     TEXT,               -- ISO datetime of last successful post
            active          INTEGER DEFAULT 1,  -- 1 = enabled, 0 = paused
            created_at      TEXT    DEFAULT (datetime('now')),
            FOREIGN KEY (guild_id) REFERENCES guild_config(guild_id)
        )
    """)

    conn.commit()
    conn.close()
    log.info("Database initialised successfully.")


# ── Convenience helpers ──────────────────────────────────────────────────────

def get_guild_config(guild_id: int) -> dict | None:
    """Fetch config row for a guild, or None if not set up yet."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return dict(row) if row else None


def upsert_guild_config(guild_id: int, **kwargs):
    """
    Insert or update guild config fields.
    Pass the fields you want to set as keyword arguments.
    Example: upsert_guild_config(123, creator_role_id=456)
    """
    config = get_guild_config(guild_id) or {}
    config["guild_id"] = guild_id
    config.update(kwargs)

    columns      = ", ".join(config.keys())
    placeholders = ", ".join("?" * len(config))
    values       = list(config.values())

    with get_connection() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO guild_config ({columns}) VALUES ({placeholders})",
            values,
        )
        conn.commit()


def is_premium(guild_id: int) -> bool:
    """Return True if the guild has premium status."""
    config = get_guild_config(guild_id)
    return bool(config and config.get("is_premium"))
