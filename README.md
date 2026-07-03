# Regulus

Trust-tier moderation bot for Discord community servers. Designed to defend against scam-account raids by auditing new joiners against configurable signals, gating channel access behind an `@Unverified` role, and escalating suspicious accounts to moderator review.

**Status: skeleton only.** The bot currently connects to Discord and logs `member_join` events to the console. Scoring, role assignment, mod-audit embeds, commands, and enforcement are not yet implemented. See [Design (planned)](#design-planned) below for the intended shape.

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

### 3. Configure

Copy the template and fill it in:

```bash
cp .env.example .env            # Windows: copy .env.example .env
```

| Variable        | Required | Description                                                                                                     |
|-----------------|----------|-----------------------------------------------------------------------------------------------------------------|
| `DISCORD_TOKEN` | yes      | Bot token from the Developer Portal.                                                                            |
| `GUILD_ID`      | yes      | Server ID. Enable Developer Mode in Discord (User Settings → Advanced), then right-click server → Copy Server ID. |

## Running

```bash
python bot.py
```

Expected startup output:

```
INFO  regulus  logged in as Regulus (id=…)
INFO  regulus    connected to guild: <server> (id=…, members=…)
```

When a member joins the configured server, a line prefixed `member joined:` is logged.

Stop the bot with `Ctrl+C`.

## Repository layout

| Path              | Purpose                                              |
|-------------------|------------------------------------------------------|
| `bot.py`          | Entry point, Discord client, event handlers.         |
| `requirements.txt`| Python dependencies.                                 |
| `.env.example`    | Template for local configuration (copy to `.env`).   |
| `.gitignore`      | Excludes `.env`, `*.db`, `__pycache__`, virtualenvs. |
| `README.md`       | This file.                                           |

---

## Design (planned)

The following describes the intended feature set. **None of it is implemented yet.** As features land, this section shrinks and the relevant behaviour moves into the operator sections above.

- **Trust bands.** New joiners are scored on account signals (age, avatar, badges, username pattern, invite used, mutual servers). Score maps to one of five bands: `Trusted` → `Likely-safe` → `Neutral` → `Suspicious` → `Malicious`, each with its own enforcement rule.
- **Hot triggers.** Behavioural shortcuts on `@Unverified` users — first message contains a Discord invite, image spam, mention attempts, phishing keywords — instantly drop the user to `Malicious` regardless of static score.
- **Personal invites.** Joining via a mod-issued personal invite is a strong trust signal, near auto-approval.
- **Mod audit embed.** Every join posts a rich embed to a moderator channel with all computed signals and `[Approve] [Deny] [Watch]` buttons; commands like `/audit @user` and `/flag @user` mirror the buttons.
- **Enforcement modes.** `ENFORCEMENT_MODE=shadow` (audit only, no actions) → `ENFORCEMENT_MODE=active` (assigns roles, kicks). A one-line flip when moving from observation to production use.
- **Local blocklist.** Manually-flagged user IDs, usernames, and avatar hashes persist across leave/rejoin and inform future scores.
- **Manual override.** Every automatic action is undoable from the audit channel; nothing is unrecoverable.
