"""
utils/database.py
==================
All SQLite database setup and shared helper functions.
Every table used by Soren is defined and created here via init_db().
"""

import sqlite3
import os
import json
import logging

log = logging.getLogger("soren.db")

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

    conn   = get_connection()
    cursor = conn.cursor()

    # ── Guild configuration ───────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id        INTEGER PRIMARY KEY,
            creator_role_id INTEGER,
            is_premium      INTEGER DEFAULT 0,
            gcal_token      TEXT,
            gcal_id         TEXT,
            embed_color     TEXT    DEFAULT '5865F2'
        )
    """)

    # ── Events ───────────────────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id              INTEGER NOT NULL,
            channel_id            INTEGER NOT NULL,
            message_id            INTEGER,
            creator_id            INTEGER NOT NULL,
            title                 TEXT    NOT NULL,
            description           TEXT,
            timezone              TEXT    DEFAULT 'UTC',
            start_time            TEXT    NOT NULL,
            end_time              TEXT,
            is_recurring          INTEGER DEFAULT 0,
            recur_rule            TEXT,
            recur_interval        INTEGER DEFAULT 1,
            recur_end             TEXT,
            parent_event_id       INTEGER,
            reminder_offset       INTEGER DEFAULT 15,
            notify_role_id        INTEGER,
            gcal_event_id         TEXT,
            reminded_at           TEXT,
            embed_color           TEXT    DEFAULT '5865F2',
            btn_accept_label      TEXT    DEFAULT '✅ Accept',
            btn_tentative_label   TEXT    DEFAULT '❓ Tentative',
            btn_decline_label     TEXT    DEFAULT '❌ Decline',
            btn_tentative_enabled INTEGER DEFAULT 1,
            max_rsvp              INTEGER DEFAULT 0,
            rsvp_cutoff           TEXT,
            created_at            TEXT    DEFAULT (datetime('now')),
            FOREIGN KEY (guild_id) REFERENCES guild_config(guild_id)
        )
    """)

    # ── Migrations: events table ──────────────────────────────────────────
    for col, definition in [
        ("btn_accept_label",      "TEXT DEFAULT '✅ Accept'"),
        ("btn_tentative_label",   "TEXT DEFAULT '❓ Tentative'"),
        ("btn_decline_label",     "TEXT DEFAULT '❌ Decline'"),
        ("btn_tentative_enabled", "INTEGER DEFAULT 1"),
        ("reminded_at",           "TEXT"),
        ("embed_color",           "TEXT DEFAULT '5865F2'"),
        ("max_rsvp",              "INTEGER DEFAULT 0"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE events ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                log.warning(f"Migration warning (events.{col}): {e}")
        except Exception as e:
            log.error(f"Unexpected migration error (events.{col}): {e}")

    # ── Migration: guild_config ───────────────────────────────────────────
    try:
        cursor.execute("ALTER TABLE guild_config ADD COLUMN embed_color TEXT DEFAULT '5865F2'")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            log.warning(f"Migration warning (guild_config.embed_color): {e}")
    except Exception as e:
        log.error(f"Unexpected migration error (guild_config.embed_color): {e}")

    # ── RSVPs ─────────────────────────────────────────────────────────────
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

    # ── Waitlist ──────────────────────────────────────────────────────────
    # Stores users waiting for a spot when an event hits max_rsvp.
    # Ordered by id (insertion order) — first in, first notified.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS waitlist (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            joined_at   TEXT    DEFAULT (datetime('now')),
            UNIQUE(event_id, user_id),
            FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
        )
    """)

    # ── G-Cal Integrations ────────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS gcal_integrations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id        INTEGER NOT NULL,
            label           TEXT    NOT NULL,
            calendar_id     TEXT    NOT NULL,
            calendar_name   TEXT,
            gcal_token      TEXT    NOT NULL,
            channel_id      INTEGER NOT NULL,
            schedule        TEXT    DEFAULT 'weekly',
            custom_interval INTEGER DEFAULT 7,
            post_day        TEXT    DEFAULT 'monday',
            post_hour       INTEGER DEFAULT 9,
            last_posted     TEXT,
            active          INTEGER DEFAULT 1,
            created_at      TEXT    DEFAULT (datetime('now')),
            FOREIGN KEY (guild_id) REFERENCES guild_config(guild_id)
        )
    """)

    # ── Migration: gcal_integrations — add calendar_name column ──────────
    try:
        cursor.execute("ALTER TABLE gcal_integrations ADD COLUMN calendar_name TEXT")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            log.warning(f"Migration warning (gcal_integrations.calendar_name): {e}")
    except Exception as e:
        log.error(f"Unexpected migration error (gcal_integrations.calendar_name): {e}")

    # ── Migration: gcal_integrations — add reminder columns ──────────────
    for col, definition in [
        ("reminders_enabled", "INTEGER DEFAULT 1"),
        ("reminder_offset",   "INTEGER DEFAULT 15"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE gcal_integrations ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                log.warning(f"Migration warning (gcal_integrations.{col}): {e}")
        except Exception as e:
            log.error(f"Unexpected migration error (gcal_integrations.{col}): {e}")

    # ── GCal integration reminders — tracks which events have been reminded ──
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS gcal_reminders (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            integration_id   INTEGER NOT NULL,
            gcal_event_id    TEXT    NOT NULL,
            fired_at         TEXT    DEFAULT (datetime('now')),
            UNIQUE(integration_id, gcal_event_id)
        )
    """)

    # ── Redeemed premium codes ────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS redeemed_codes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            code            TEXT    NOT NULL UNIQUE,
            used_by_guild   INTEGER NOT NULL,
            used_at         TEXT    DEFAULT (datetime('now'))
        )
    """)

    # ── Modlogs config ────────────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS modlogs_config (
            guild_id    INTEGER PRIMARY KEY,
            channel_id  INTEGER NOT NULL,
            enabled     INTEGER DEFAULT 1
        )
    """)

    # ── Migration: events — notify_role_ids ───────────────────────────────
    try:
        cursor.execute("ALTER TABLE events ADD COLUMN notify_role_ids TEXT")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            log.warning(f"Migration warning (events.notify_role_ids): {e}")
    except Exception as e:
        log.error(f"Unexpected migration error (events.notify_role_ids): {e}")

    # ── Migration: events — rsvp_cutoff ──────────────────────────────────
    try:
        cursor.execute("ALTER TABLE events ADD COLUMN rsvp_cutoff TEXT")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            log.warning(f"Migration warning (events.rsvp_cutoff): {e}")
    except Exception as e:
        log.error(f"Unexpected migration error (events.rsvp_cutoff): {e}")

    # ── Migration: events — is_cancelled ──────────────────────────────────
    try:
        cursor.execute("ALTER TABLE events ADD COLUMN is_cancelled INTEGER DEFAULT 0")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            log.warning(f"Migration warning (events.is_cancelled): {e}")
    except Exception as e:
        log.error(f"Unexpected migration error (events.is_cancelled): {e}")

    # Backfill: mark any events whose title starts with [CANCELLED]
    cursor.execute("UPDATE events SET is_cancelled=1 WHERE title LIKE '[CANCELLED]%' AND is_cancelled=0")

    conn.commit()
    conn.close()
    log.info("Database initialised successfully.")


# ── Convenience helpers ───────────────────────────────────────────────────────

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


def parse_role_ids(event: dict) -> list[int]:
    """
    Return notify role IDs for an event as a list of ints.
    Reads notify_role_ids (JSON array) first; falls back to the
    legacy notify_role_id integer column for older rows.
    """
    raw = event.get("notify_role_ids")
    if raw:
        try:
            ids = json.loads(raw)
            if isinstance(ids, list) and ids:
                return [int(i) for i in ids if i]
        except (json.JSONDecodeError, ValueError):
            pass
    legacy = event.get("notify_role_id")
    if legacy:
        return [int(legacy)]
    return []