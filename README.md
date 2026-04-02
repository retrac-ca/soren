# Soren — Discord Calendar & Events Bot

Soren is a self-hosted Discord bot for creating and managing events with RSVP signups, recurring schedules, pre-event reminders, and optional Google Calendar sync. Designed for clans, guilds, and communities who want full ownership of their events inside Discord.

**Creator:** Toadle
**Support:** https://soren.retrac.ca
**GitHub:** https://github.com/retrac-ca/soren
**Invite Soren:** https://discord.com/oauth2/authorize?client_id=1423474696783138839&permissions=17929878105152&integration_type=0&scope=bot
**Version:** 1.3

---

## Features

**Event Management**
- Create single or recurring events (daily, weekly, bi-weekly, bi-monthly, monthly, or custom interval)
- Edit event details and timing separately via dedicated commands
- Soft cancel events without losing the embed
- Export upcoming events as a standard `.ics` file for import into any calendar app

**RSVP System**
- Accept, Tentative, and Decline buttons on every event embed
- Live embed updates as members RSVP
- Configurable max capacity with automatic waitlist and notifications
- Customizable button labels per event

**Reminders**
- Configurable reminder timing per event (15 min, 30 min, 1 hour, or custom)
- Pings a designated notify role and all accepted/tentative RSVPers
- Reminder state is tracked in the database — reminders survive bot restarts and are never sent twice

**Google Calendar**
- Two-way sync: push Discord events to Google Calendar, pull Google Calendar events into Discord
- Multi-calendar digest summaries: connect multiple calendars, each auto-posting weekly summaries to a channel on a configurable schedule
- Paginated summaries (8 events per page) with navigation buttons

**Customization**
- Per-server embed color (3 colors free, 8 colors premium)
- Fully customizable RSVP button labels

**Free vs Premium**

| Feature | Free | Premium |
|---|---|---|
| Events per server | 10 | Unlimited |
| RSVP names shown per event | 50 | Unlimited |
| Embed color options | 3 | 8 |
| Recurring events | Yes | Yes |
| Google Calendar sync | Yes | Yes |
| G-Cal integrations | Up to 5 | Unlimited |
| Custom button labels | Yes | Yes |
| Pricing | Free | $15 one-time per server |

---

## Requirements

- Python 3.10 or newer
- A Discord bot application ([create one here](https://discord.com/developers/applications))
- Google Cloud project with Calendar API enabled *(optional — only needed for Google Calendar features)*

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/retrac-ca/soren.git
cd soren
```

### 2. Create a virtual environment

```bash
python -m venv venv

# macOS / Linux
source venv/bin/activate

# Windows
venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in your values:

```
DISCORD_TOKEN=your_bot_token_here
BOT_OWNER_ID=your_discord_user_id_here
```

### 5. Discord Developer Portal settings

1. Go to your application at https://discord.com/developers/applications
2. Under **Bot**, enable **Server Members Intent**
3. Under **OAuth2 → URL Generator**, select scopes: `bot` and `applications.commands`
4. Select bot permissions: `Send Messages`, `Embed Links`, `Read Message History`, `Mention Everyone`, `Attach Files`
5. Use the following invite link to add Soren to your server:
   ```
   https://discord.com/oauth2/authorize?client_id=1423474696783138839&permissions=17929878105152&integration_type=0&scope=bot
   ```

### 6. Run the bot

```bash
python bot.py
```

---

## First-Time Server Setup

1. Run `/setup` as a server administrator and select the **Event Creator role**
2. Optionally run `/embedcolor` to choose your server's embed color
3. Run `/help` to see all available commands

---

## Creating an Event

Run `/newevent #channel`. The flow has four steps:

1. **Choose event type** — Single, Daily, Weekly, Bi-Weekly, Bi-Monthly, Monthly, or Custom
2. **Choose timezone** — Dropdown of common timezones
3. **Choose reminder timing** — 15 min, 30 min, 45 min, 1 hour, 2 hours, or Custom
4. **Fill in the details** — Title, description, start/end time, notify role (optional)

**Accepted date/time formats:**

```
2026-07-04 20:00
July 4 8pm
next Friday 9pm
Apr 2 7:30pm
tomorrow 8pm
```

**Editing an event after creation:**

- `/editeventdetails` — Edit title, description, max RSVPs, or notify role
- `/editeventtime` — Edit start/end time, timezone, or reminder offset

---

## Google Calendar Setup

### Step 1 — Google Cloud Console (one-time)

1. Go to https://console.cloud.google.com and create a project
2. Enable the **Google Calendar API** under APIs & Services → Library
3. Configure the **OAuth consent screen** (External; add `calendar.readonly` scope for integrations, `calendar` scope for full sync)
4. Create credentials: **OAuth 2.0 Client ID** → **Web application**
   - Add authorized redirect URI: `http://localhost`
5. Download the JSON file, rename it to `google_credentials.json`, and place it in the Soren root folder

> `google_credentials.json` is gitignored and must never be committed to version control.

### Step 2 — Connect in Discord

**For two-way sync (pushes /newevent events to Google Calendar):**
```
/gcal connect → authorize → /gcal verify <code>
```

**For multi-calendar digest summaries:**
```
/gcalint add → choose schedule → fill in details → authorize → /gcalint verify <code> → pick calendar
```

When authorizing, your browser will redirect to a `localhost` page that shows "This site can't be reached" — this is normal. Copy the `code=` value from the URL bar and paste it into the verify command.

---

## Command Reference

### Setup (Admins)

| Command | Description |
|---|---|
| `/setup` | First-time server configuration |
| `/config` | View current server settings |
| `/embedcolor` | Choose the event embed color |

### Events (Event Creator role required)

| Command | Description |
|---|---|
| `/newevent` | Create a new event |
| `/editeventdetails` | Edit title, description, max RSVPs, notify role |
| `/editeventtime` | Edit start/end time, timezone, reminder |
| `/deleteevent` | Permanently delete an event |
| `/cancelevent` | Soft cancel an event (marks as cancelled, keeps embed) |
| `/listevents` | View all upcoming events in this server |
| `/myevents` | View events you have RSVPed to |
| `/eventbuttons` | Customize RSVP button labels and toggle Tentative button |
| `/exportevents` | Export upcoming events as a .ics calendar file |

### Google Calendar (Admins)

| Command | Description |
|---|---|
| `/gcal connect` | Connect a Google Calendar for two-way sync |
| `/gcal verify` | Complete the Google Calendar connection |
| `/gcal disconnect` | Remove the Google Calendar connection |

### G-Cal Integrations (Admins)

| Command | Description |
|---|---|
| `/gcalint add` | Connect a Google Calendar for auto-posting summaries |
| `/gcalint verify` | Complete the auth and pick which calendar to connect |
| `/gcalint list` | View all connected calendar integrations |
| `/gcalint remove` | Disconnect a calendar integration |
| `/gcalint pause` | Pause or resume a calendar integration |
| `/gcalint post` | Manually trigger a summary post right now |

### Premium & Info

| Command | Description |
|---|---|
| `/premiumcode` | Redeem a premium code (admin only) |
| `/premium` | View free vs premium feature comparison |
| `/ping` | Check bot status, latency, and uptime |
| `/help` | Show all commands |

---

## Hosting with systemd (Linux VPS)

To keep Soren running after you disconnect and have it restart automatically on crash or reboot:

```bash
sudo nano /etc/systemd/system/soren.service
```

```ini
[Unit]
Description=Soren Discord Bot
After=network.target

[Service]
Type=simple
User=YOUR_LINUX_USERNAME
WorkingDirectory=/path/to/soren
ExecStart=/path/to/soren/venv/bin/python bot.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable soren
sudo systemctl start soren
```

Check status:
```bash
sudo systemctl status soren
```

---

## Logs

Soren writes logs to both the console and monthly rotating files in the `logs/` directory.

```
logs/
  soren_2026_04.log
  soren_2026_05.log
  ...
```

Files are retained for 24 months. The `logs/` directory is gitignored.

---

## Project Structure

```
soren/
├── bot.py                      # Entry point
├── requirements.txt            # Python dependencies
├── .env.example                # Environment variable template
├── .env                        # Your secrets (never commit this)
├── .gitignore
├── google_credentials.json     # Google OAuth credentials (never commit this)
├── premium_keys.txt            # Premium redemption codes (never commit this)
│
├── cogs/
│   ├── setup.py                # /setup, /config, /embedcolor
│   ├── events.py               # Event creation and management commands
│   ├── rsvp.py                 # RSVP buttons and embed refresh logic
│   ├── reminders.py            # Background reminder loop and OAuth token refresh
│   ├── google_cal.py           # Primary Google Calendar sync (/gcal)
│   ├── gcal_integrations.py    # Multi-calendar weekly summaries (/gcalint)
│   ├── premium.py              # /premiumcode, /premium, /help
│   └── ping.py                 # /ping — bot status and latency
│
├── utils/
│   ├── database.py             # SQLite setup, schema, and helpers
│   ├── embeds.py               # Discord embed builders and color constants
│   └── permissions.py          # Event creator role checks
│
├── logs/                       # Monthly rotating log files (gitignored)
└── data/
    └── soren.db                # SQLite database (gitignored)
```

---

## Security Notes

The following files must never be committed to version control. All three are included in `.gitignore`:

- `.env` — contains your Discord bot token
- `google_credentials.json` — contains your Google OAuth client secret
- `premium_keys.txt` — contains your premium redemption codes

If any of these files are accidentally committed, rotate the affected credentials immediately.

---

## Premium

Soren Premium is a one-time purchase of $15 per server with no subscriptions or renewals. Purchase at **https://soren.retrac.ca**. You will receive a code to activate with `/premiumcode` in your server.

Premium codes are managed in `premium_keys.txt` — one code per line. This file is gitignored and should never be committed.

---

## License

MIT License — free to use, modify, and distribute.

---

## Support

Visit **https://soren.retrac.ca** or contact **info@retrac.ca**.

**Invite Soren to your server:** https://discord.com/oauth2/authorize?client_id=1423474696783138839&permissions=17929878105152&integration_type=0&scope=bot