# Regulus

Trust-tier moderation bot for Discord community servers. Designed to defend against scam-account raids by auditing new joiners against configurable signals, gating channel access behind an `@Unverified` role, and escalating suspicious accounts to moderator review.

**Status: shadow-mode audit.** On member join the bot computes a trust score from account signals (age, avatar, animated-avatar, banner, avatar decoration, server-boost status, public flags, username pattern), maps it to a band, and posts an audit embed to a configured moderator channel. Moderators can also rerun the audit against any member on demand via `/audit @user` or the right-click **Apps → Audit user** menu (both are mod-only and reply ephemerally). **The score is informational only — no roles are assigned, no automatic actions are taken.** See [Design (planned)](#design-planned) below for the enforcement layer and everything else still on the roadmap.

---

## Requirements

- Python 3.11 or newer
- A Discord application with a bot user ([Developer Portal](https://discord.com/developers/applications))
- A server where you can invite bots and manage roles

## Setup

### 1. Clone and install

```bash
git clone https://github.com/Acrith/regulus.git
cd regulus
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Create the Discord application

In the [Developer Portal](https://discord.com/developers/applications):

1. **New Application** → name it whatever you like.
2. **Bot** tab → enable both privileged intents:
   - `SERVER MEMBERS INTENT`
   - `MESSAGE CONTENT INTENT`
3. **Bot** tab → **Reset Token** → copy it. Treat this like a password.
4. **OAuth2 → URL Generator** → tick scopes `bot` and `applications.commands`, tick permission `Administrator` (for development; narrow later). Open the generated URL, pick your server, authorize.

### 3. Create the mod-audit channel

In your server, create a text channel (e.g. `#mod-audit`), restrict it to moderator roles, and make sure the bot can view and send messages there (Administrator covers this during development). Enable Developer Mode in Discord (User Settings → Advanced), then right-click the channel → Copy Channel ID.

### 4. Configure

Copy the template and fill it in:

```bash
cp .env.example .env            # Windows: copy .env.example .env
```

| Variable         | Required | Description                                                                                                     |
|------------------|----------|-----------------------------------------------------------------------------------------------------------------|
| `DISCORD_TOKEN`  | yes      | Bot token from the Developer Portal.                                                                            |
| `GUILD_ID`       | yes      | Server ID. Enable Developer Mode in Discord (User Settings → Advanced), then right-click server → Copy Server ID. |
| `MOD_CHANNEL_ID` | yes      | Channel ID for the mod-audit channel (see Setup step 3). Audit embeds are posted here on each member join.      |

## Running

```bash
python bot.py
```

Expected startup output:

```
INFO  regulus  logged in as Regulus (id=…)
INFO  regulus    connected to guild: <server> (id=…, members=…)
INFO  regulus    mod channel: #<name> (id=…)
```

When a member joins the configured server, the bot:

1. Fetches the full User object once (needed for banner and some profile data not present on the cached Member).
2. Computes a trust score from account signals and picks a band.
3. Logs the join to the console: `member joined: <user> (id=…, score=…, band=…)`.
4. Posts an audit embed to `MOD_CHANNEL_ID` showing every signal, the total score, and the band.

Bands, from highest score to lowest: `Trusted`, `Likely-safe`, `Neutral`, `Suspicious`, `Malicious`. Thresholds are defined in `scoring.py` and are the initial guess — expect to tune them.

Current signals, defined in `scoring.py`:

| Signal            | Reads                                | Weight range       | Notes                                                              |
|-------------------|--------------------------------------|--------------------|--------------------------------------------------------------------|
| Account age       | `member.created_at`                  | −3 to +2           | Days since Discord account creation.                               |
| Avatar            | `member.avatar` + `is_animated()`    | −2, +1, or +2      | Default: −2. Static custom: +1. Animated (Nitro-only): +2.         |
| Banner            | `full_user.banner`                   | 0 or +1            | Custom banner requires Nitro; weak positive.                       |
| Avatar decoration | `member.avatar_decoration`           | 0 or +1            | Overlay around the avatar; Nitro-only.                             |
| Server booster    | `member.premium_since`               | 0 or +3            | Boosting the current guild — strong positive.                      |
| Public flags      | `member.public_flags`                | −1 to +3           | HypeSquad / Nitro Early / Active Developer / etc. Limited set exposed by the API. |
| Username pattern  | regex on `member.name`               | −2 or 0            | Trailing `\d{4,}$` — the `word####` scam signature.                |

The **Discord API deliberately hides several profile signals from bots** (connections such as Twitch or X, nameplate, display-name colour, profile widgets, current Nitro subscription state for other users). Static scoring therefore has a real ceiling; behavioural triggers and a local blocklist (see below) will close the gap for well-disguised accounts.

Stop the bot with `Ctrl+C`.

## Moderator commands

Available to any user with the `Manage Messages` permission (adjustable per-command in **Server Settings → Integrations → Regulus**). Both reply ephemerally so the target member cannot see the response.

| Trigger                                 | Effect                                                            |
|-----------------------------------------|-------------------------------------------------------------------|
| `/audit member:@user`                   | Runs the trust audit on `@user` and returns the audit embed.      |
| Right-click a user → **Apps → Audit user** | Same as `/audit`, invoked via context menu.                     |

Commands are guild-scoped and sync at startup — they appear in Discord within a second or two of the bot connecting.

## Repository layout

| Path              | Purpose                                                          |
|-------------------|------------------------------------------------------------------|
| `bot.py`          | Entry point, Discord client, event handlers.                     |
| `scoring.py`      | Signal functions, `Audit` dataclass, score → band mapping.       |
| `embeds.py`       | Discord embed formatting for the mod-audit channel.              |
| `requirements.txt`| Python dependencies.                                             |
| `.env.example`    | Template for local configuration (copy to `.env`).               |
| `.gitignore`      | Excludes `.env`, `*.db`, `__pycache__`, virtualenvs.             |
| `README.md`       | This file.                                                       |

---

## Design (planned)

The following are still to be built. As features land, bullets move out of here and into the operator sections above.

- **Enforcement.** An `ENFORCEMENT_MODE=active` env var will flip on: assigning `@Unverified` on join, holding low-band members off channels until promoted, and auto-kicking `Malicious`. `shadow` (current implicit behaviour) stays available for observation.
- **Hot triggers.** Behavioural shortcuts on `@Unverified` users — first message contains a Discord invite, image spam, mention attempts, phishing keywords — will instantly drop the user to `Malicious` regardless of static score.
- **Additional signals.** Invite used, mutual-server count, banner presence, blocklist match. All wire into the same `Audit` structure and appear in the same embed.
- **Personal invites.** Joining via a mod-issued personal invite will contribute a strong positive weight, near auto-approval.
- **Interactive buttons.** The audit embed will grow `[Approve] [Deny] [Watch]` buttons wired to bot actions.
- **More commands.** `/flag @user reason` adds to the local blocklist; `/approve @user`, `/deny @user`, `/watchlist` mirror the buttons.
- **Local blocklist.** Manually-flagged user IDs, usernames, and avatar hashes persist to SQLite across leave/rejoin and inform future scores.
- **Manual override.** Every automatic action will be undoable from the audit channel.
