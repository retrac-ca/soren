# Privacy Policy

**Soren Discord Bot**
Operated by Toadle
Contact: info@retrac.ca
Last updated: April 2026

---

## 1. Overview

This Privacy Policy describes how Soren ("we", "us", "our") collects, uses, and stores information when you use the Soren Discord bot. We are committed to protecting your privacy and handling your data responsibly in accordance with Canada's Personal Information Protection and Electronic Documents Act (PIPEDA).

---

## 2. What Information We Collect

Soren collects and stores only the minimum information required to provide its functionality.

### Information collected automatically when you use Soren:

| Data | Why it is stored |
|---|---|
| Your Discord user ID | To record your RSVP status on events and manage waitlist entries |
| Your Discord guild (server) ID | To scope all data to the correct server |
| Your Discord display name | Shown in the RSVP list on event embeds at the time of display (not stored separately) |
| Event details you create | Title, description, date/time, timezone, reminder offset — stored to power the event system |
| RSVP status | Whether you have accepted, declined, or tentatively accepted an event |
| Google OAuth tokens | Only if you voluntarily connect a Google Calendar; stored to maintain the integration |

### Information we do NOT collect:
- Your Discord username or email address
- Your IP address
- Payment information (payments are handled externally)
- Any message content outside of slash command inputs

---

## 3. How We Use Your Information

We use the information we collect solely to operate Soren's features:

- To display event information and RSVP lists in Discord
- To send event reminders to the correct channels and roles
- To authenticate with Google Calendar on your behalf (if you connect an integration)
- To enforce free tier limits and validate premium status per server

We do not use your data for advertising, profiling, or any purpose beyond operating the bot.

---

## 4. Data Storage

All data is stored in a SQLite database on the server that hosts Soren. This server is located in Canada or within a jurisdiction that provides adequate data protection under PIPEDA.

Data is not shared with any third party except:
- **Google LLC** — if you voluntarily connect a Google Calendar integration, OAuth tokens are exchanged with Google's authentication servers. Google's privacy policy applies to that exchange.
- **Discord Inc.** — Soren operates on Discord's platform and all interactions pass through Discord's infrastructure. Discord's privacy policy applies.

---

## 5. Data Retention

Your data is retained for as long as it is needed to operate the features you use:

- **Event and RSVP data** is retained until the event is deleted by a server administrator or until Soren is removed from the server
- **Google OAuth tokens** are retained until you disconnect the integration via `/gcalint remove` or `/gcal disconnect`, or until you revoke access through your Google account
- **Premium redemption records** are retained indefinitely to prevent code reuse

When Soren is removed from a Discord server, we make no automatic guarantee that the server's data is immediately deleted, but we will delete it upon request.

---

## 6. Your Rights Under PIPEDA

As a Canadian privacy law, PIPEDA gives you the right to:

- **Know** what personal information we hold about you
- **Access** your personal information upon request
- **Correct** inaccurate personal information
- **Withdraw consent** and request deletion of your data

To exercise any of these rights, contact us at **info@retrac.ca**. We will respond within 30 days.

Because Soren stores data by Discord user ID (not by name or email), please include your Discord user ID in any data request so we can locate your records accurately.

---

## 7. Children's Privacy

Soren is not directed at children under the age of 13. We do not knowingly collect personal information from children under 13. If you believe a child under 13 has used Soren and their data has been collected, contact us at info@retrac.ca and we will delete it.

---

## 8. Security

We take reasonable technical measures to protect the data we store, including keeping credentials out of version control and restricting access to the hosting environment. However, no system is completely secure, and we cannot guarantee absolute security of your data.

---

## 9. Changes to This Policy

We may update this Privacy Policy from time to time. The "Last updated" date at the top of this page will reflect any changes. Continued use of Soren after changes are posted constitutes acceptance of the revised policy.

---

## 10. Contact

For privacy-related questions, data access requests, or deletion requests, contact us at **info@retrac.ca**.