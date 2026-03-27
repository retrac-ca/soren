# 📅 Soren — Discord Calendar & Events Bot

Soren is a self-hosted Discord bot for creating and managing events with RSVP signups, recurring schedules, pre-event reminders, and optional Google Calendar sync. Designed for clans, guilds, and communities who want full ownership of their events inside Discord.

---

## ✨ Features

| Feature | Free | Premium |
|---|---|---|
| Events per server | 5 | Unlimited |
| RSVP list per event | 50 shown | Unlimited |
| Single & recurring events | ✅ | ✅ |
| Accept / Tentative / Decline signups | ✅ | ✅ |
| Pre-event reminders (configurable) | ✅ | ✅ |
| Google Calendar sync (/gcal) | ✅ | ✅ |
| G-Cal Integrations (multi-calendar summaries) | ✅ | ✅ |

---

## 📁 Project Structure

```
soren/
├── bot.py                      # Entry point — starts the bot
├── requirements.txt            # Python dependencies
├── .env.example                # Environment variable template
├── .env                        # Your secrets (never commit this)
├── .gitignore
├── google_credentials.json     # Google OAuth credentials (never commit this)
│
├── cogs/
│   ├── setup.py                # /setup, /config, /setpremium
│   ├── events.py               # /newevent, /editevent, /deleteevent, /listevents
│   ├── rsvp.py                 # RSVP buttons and embed refresh logic
│   ├── reminders.py            # Background reminder loop
│   ├── google_cal.py           # Primary Google Calendar sync (/gcal)
│   ├── gcal_integrations.py    # Multi-calendar weekly summaries (/gcalint)
│   └── premium.py              # /premium, /help
│
├── utils/
│   ├── database.py             # SQLite setup, schema, and query helpers
│   ├── embeds.py               # Discord embed builders
│   └── permissions.py         # Event creator role checks
│
└── data/
    └── soren.db                # SQLite database (gitignored)
```

---

## 🚀 Installation

### Prerequisites
- Python 3.10 or newer
- A Discord bot application ([create one here](https://discord.com/developers/applications))

### Step 1 — Clone the repository
```bash
git clone https://github.com/YOUR_USERNAME/soren.git
cd soren
```

### Step 2 — Create a virtual environment
```bash
python -m venv venv
source venv/bin/activate        # macOS / Linux
venv\Scripts\activate           # Windows
```

### Step 3 — Install dependencies
```bash
pip install -r requirements.txt
```

### Step 4 — Configure environment variables
```bash
cp .env.example .env
```
Open `.env` and fill in:
```
DISCORD_TOKEN=your_bot_token_here
BOT_OWNER_ID=your_discord_user_id_here
```

### Step 5 — Discord Developer Portal settings
1. Go to **Bot** → enable **Server Members Intent**
2. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Embed Links`, `Read Message History`, `Mention Everyone`
3. Copy the generated URL and invite Soren to your server.

### Step 6 — Run the bot
```bash
python bot.py
```

---

## ⚙️ First-Time Server Setup

1. Run `/setup` as a server administrator
2. Select the **Event Creator role**
3. Done — use `/help` to see all commands

---

## 📅 Creating an Event

Run `/newevent #channel`. The flow has **3 steps:**

1. **Choose event type** — Single, Daily, Weekly, Bi-Weekly, Bi-Monthly, Monthly, or Custom
2. **Choose timezone** — Dropdown of North American timezones + UTC
3. **Fill in the details** — Title, description, start/end time

### Timezone Options

| Label | Timezone | Notes |
|---|---|---|
| Eastern Time (ET) | America/New_York | UTC-5 / UTC-4 DST |
| Central Time (CT) | America/Chicago | UTC-6 / UTC-5 DST |
| Mountain Time (MT) | America/Denver | UTC-7 / UTC-6 DST |
| Mountain Time - AZ | America/Phoenix | UTC-7, no DST |
| Pacific Time (PT) | America/Los_Angeles | UTC-8 / UTC-7 DST |
| Alaska Time (AKT) | America/Anchorage | UTC-9 / UTC-8 DST |
| Hawaii Time (HT) | Pacific/Honolulu | UTC-10, no DST |
| Atlantic Time (AT) | America/Halifax | UTC-4 / UTC-3 DST |
| Newfoundland Time (NT) | America/St_Johns | UTC-3:30 / UTC-2:30 DST |
| UTC | UTC | Coordinated Universal Time |

---

## 📆 Google Calendar Setup

### Google Cloud Console (one-time setup)

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create a project
2. Enable the **Google Calendar API** (APIs & Services → Library)
3. Configure the **OAuth consent screen** (External, add `calendar.readonly` scope)
4. Create credentials: **OAuth client ID** → **Web application**
   - Authorized redirect URI: `http://localhost`
5. Download the JSON, rename it `google_credentials.json`, place it in the Soren root folder

> ⚠️ Never commit `google_credentials.json` — it's already in `.gitignore`

---

### G-Cal Integrations (`/gcalint`) — Multi-calendar summaries

Connect multiple Google Calendars, each auto-posting weekly digests to a Discord channel.

**Setup flow:**
1. `/gcalint add` → choose schedule → choose day (weekly) → fill in label/channel/hour
2. Click the auth link → authorize with Google → browser shows "This site can't be reached" (normal!)
3. Copy the `code=` value from the URL bar: `http://localhost/?code=COPY_THIS&scope=...`
4. `/gcalint verify <code>` → a **calendar picker** appears — choose which calendar to connect
5. Done!

| Command | Description |
|---|---|
| `/gcalint add` | Connect a new calendar |
| `/gcalint verify <code>` | Complete auth + pick which calendar |
| `/gcalint list` | Show all connected calendars |
| `/gcalint remove <id>` | Disconnect a calendar |
| `/gcalint pause <id>` | Pause/resume auto-posting |
| `/gcalint post <id>` | Manually trigger a summary now |

---

### Primary Sync (`/gcal`) — Single calendar, two-way

| Command | Description |
|---|---|
| `/gcal connect` | Connect primary Google Calendar |
| `/gcal verify <code>` | Complete auth |
| `/gcal disconnect` | Remove the connection |

Events from `/newevent` are pushed to Google Calendar. New Google Calendar events are pulled into Discord every 15 minutes (no RSVP buttons).

---

## 🔔 Reminders

Soren sends reminders **15 minutes before** each event by default. Change the offset via the ✏️ Edit button or `/editevent`. Reminders ping the notify role and all Accepted/Tentative RSVPers.

---

## 💎 Premium

Free servers: **5 events max**, **50 RSVP names shown**. Bot owner enables premium with:
```
/setpremium enabled:True
```

---

## 🖥️ Hosting

### systemd (recommended for Linux VPS)

Create `/etc/systemd/system/soren.service`:
```ini
[Unit]
Description=Soren Discord Bot
After=network.target

[Service]
Type=simple
User=YOUR_LINUX_USER
WorkingDirectory=/path/to/soren
ExecStart=/path/to/soren/venv/bin/python bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable soren && sudo systemctl start soren
```

---

## 📋 Full Command Reference

| Command | Who | Description |
|---|---|---|
| `/setup` | Admins | First-time server configuration |
| `/config` | Admins | View current settings |
| `/setpremium` | Bot owner | Toggle premium for a server |
| `/newevent` | Event Creator | Create a new event |
| `/editevent` | Event Creator | Edit an event by ID |
| `/deleteevent` | Event Creator | Delete an event |
| `/listevents` | Everyone | View upcoming events |
| `/gcal connect` | Admins | Connect primary Google Calendar |
| `/gcal verify` | Admins | Complete primary Google Calendar auth |
| `/gcal disconnect` | Admins | Remove primary calendar link |
| `/gcalint add` | Admins | Connect a calendar for auto-summaries |
| `/gcalint verify` | Admins | Complete auth + pick which calendar |
| `/gcalint list` | Admins | List all connected calendars |
| `/gcalint remove` | Admins | Disconnect a calendar |
| `/gcalint pause` | Admins | Pause/resume a calendar |
| `/gcalint post` | Admins | Manually post a summary now |
| `/premium` | Everyone | Free vs Premium comparison |
| `/help` | Everyone | Full command list |

---

## 🛠️ Development Notes

- **Language:** Python 3.10+
- **Discord library:** [py-cord](https://docs.pycord.dev/)
- **Database:** SQLite — one `soren.db` per deployment, all data scoped by `guild_id`
- **Multi-server safe:** Designed to run on many servers simultaneously
- **Architecture:** Cog-based — each feature in its own file under `cogs/`

---

## 🔒 Security

Never commit these files:
- `.env` — Discord bot token
- `google_credentials.json` — Google OAuth client secret

Both are in `.gitignore` already.

---

## 📄 License

MIT License — free to use, modify, and distribute.
