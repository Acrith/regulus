# Regulus

Trust-tier moderation bot for Discord community servers. Designed to defend against scam-account raids by auditing new joiners against configurable signals, gating channel access behind an `@Unverified` role, and escalating suspicious accounts to moderator review.

**Status: shadow-mode audit + local blocklist + invite tracking + behavioural signals.** On member join the bot computes a trust score from twelve signals (blocklist match, invite used, mutual servers with the bot, account age, avatar, banner, avatar decoration, server-boost status, public flags, username pattern, onboarding speed, first-message timing), maps it to a band, and posts an audit embed to a configured moderator channel. Behavioural signals update as events unfold — when a member completes Onboarding and when they send their first message, the mod channel gets a notice plus an updated audit embed reflecting the new information. Every audit — automatic or manual — is persisted to a local SQLite database. Moderators can `/flag @user` to add someone to the blocklist (auto-Malicious on future audits) or `/unflag @user` to remove them. Manual audits are available via `/audit @user` or the right-click **Apps → Audit user** menu. **The score is informational only — no roles are assigned, no automatic actions are taken.** See [Design (planned)](#design-planned) below for enforcement and everything else still on the roadmap.

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

For **each** server the bot will operate in, create a text channel (e.g. `#mod-audit`), restrict it to moderator roles, and make sure the bot can view and send messages there (Administrator covers this during development). Enable Developer Mode in Discord (User Settings → Advanced), then right-click each channel → Copy Channel ID. Also copy each server's ID (right-click server icon → Copy Server ID).

### 4. Configure

Copy the template and fill it in:

```bash
cp .env.example .env            # Windows: copy .env.example .env
```

| Variable        | Required | Description                                                                              |
|-----------------|----------|------------------------------------------------------------------------------------------|
| `DISCORD_TOKEN` | yes      | Bot token from the Developer Portal.                                                     |
| `GUILDS`        | yes      | Comma-separated pairs of `guild_id:mod_channel_id`. See below.                           |
| `DB_PATH`       | no       | Path to the SQLite database file. Defaults to `regulus.db` alongside `bot.py`.           |

#### Multi-server setup

`GUILDS` lists every server the bot should operate in, each paired with the mod-audit channel for that server:

```
GUILDS=1234567890:9876543210,2222222222:3333333333
```

- Format is `guild_id:mod_channel_id`, pairs comma-separated. Whitespace is tolerated.
- The bot **ignores any server it happens to be in but that is not listed here** — no commands sync, no join audits are posted. This is safe by default: adding the bot to a new server has no effect until you list it.
- A single server is a valid config (one pair, no comma).

## Running

```bash
python bot.py
```

Expected startup output (with two configured servers):

```
INFO  regulus  synced 2 slash command(s) to guild 1234567890
INFO  regulus  synced 2 slash command(s) to guild 2222222222
INFO  regulus  logged in as Regulus (id=…)
INFO  regulus    guild: Test Server (id=1234567890, members=3)
INFO  regulus      mod channel: #mod-audit (id=9876543210)
INFO  regulus    guild: Live Server (id=2222222222, members=530)
INFO  regulus      mod channel: #mod-audit (id=3333333333)
```

If the bot is in a server that is not listed in `GUILDS`, it is logged as `(not configured — events ignored)`.

On every startup the bot **bootstraps** the `members` table by walking each configured guild's current member list and creating a row for anyone missing one, using Discord's canonical `member.joined_at`. Invite is left null (we never observed the diff). Onboarding completion is not backfilled with a timestamp — instead the `Onboarding speed` signal detects "completed but timing missed" from the current member state and reports honestly. Bootstrap runs quickly (a few hundred members complete in under a second) and is safe to re-run — it only touches members with no existing record.

When a member joins a **configured** server, the bot:

1. Identifies which invite was used (invite-uses diff against the cache).
2. Records the join into the `members` table with `joined_at` and `invite_code`.
3. Fetches the full User object (needed for banner and some profile data not present on the cached Member).
4. Computes a trust score from all signals and picks a band. Behavioural signals (onboarding speed, first-message timing) read `pending` / `none yet` at this point.
5. Logs to console and posts an audit embed to the guild's configured mod channel.

Then, as behaviour unfolds:

- **Onboarding completed** (either Rules Screening's `pending` transition or the `COMPLETED_ONBOARDING` member flag) → `onboarding_completed_at` is set; a small notice plus an **updated** audit embed lands in the mod channel with the elapsed duration and a `**speedrun**` / `fast` marker for suspicious times.
- **First message sent** → `first_message_at` is set; a notice with the message preview (and any attachment/embed count) plus an updated audit embed lands in the mod channel. A first message within 30 seconds of join is flagged `**immediately after join**`.

Bands, from highest score to lowest: `Trusted`, `Likely-safe`, `Neutral`, `Suspicious`, `Malicious`. Thresholds are defined in `scoring.py` and are the initial guess — expect to tune them.

Current signals, defined in `scoring.py`:

| Signal              | Reads                                | Weight range       | Notes                                                                                              |
|---------------------|--------------------------------------|--------------------|----------------------------------------------------------------------------------------------------|
| Blocklist           | local SQLite `flags` table           | 0 or −10           | Overrides everything: a flag drops the user to Malicious. Set via `/flag`.                        |
| Invite              | per-guild invite cache               | 0 (informational)  | Which invite was used, by whom, use count. `unknown` on vanity URL, cold cache, or `/audit` runs.  |
| Mutual servers      | `member.mutual_guilds` + `bot.guilds` | 0 (informational)  | Shared server count with the bot (excluding the current guild). Currently unweighted — penalising 0 mutuals only makes sense once the bot is in several sister communities. Re-enable a negative weight for 0 mutuals when the bot is running on 4+ sister servers. |
| Account age         | `member.created_at`                  | −3 to +2           | Days since Discord account creation.                                                               |
| Avatar              | `member.avatar` + `is_animated()`    | −2, +1, or +2      | Default: −2. Static custom: +1. Animated (Nitro-only): +2.                                         |
| Banner              | `full_user.banner`                   | 0 or +1            | Custom banner requires Nitro; weak positive.                                                       |
| Avatar decoration   | `member.avatar_decoration`           | 0 or +1            | Overlay around the avatar; Nitro-only.                                                             |
| Server booster      | `member.premium_since`               | 0 or +3            | Boosting the current guild — strong positive.                                                      |
| Public flags        | `member.public_flags`                | −1 to +3           | HypeSquad / Nitro Early / Active Developer / etc. Limited set exposed by the API.                 |
| Username pattern    | regex on `member.name`               | −2 or 0            | Trailing `\d{4,}$` — the `word####` scam signature.                                                |
| Onboarding speed    | `members.onboarding_completed_at`    | −3 to +1           | Seconds between join and onboarding completion. <5s: speedrun. <30s: fast. 30s–30m: normal. >30m: deliberate. If the member state shows completion but the bot missed the event (was down), the signal reports so honestly at weight 0. |
| First message timing | `members.first_message_at`          | −2 or 0            | Seconds between join and first message. <30s: posted immediately (bot-like). Otherwise neutral for now — content-based scoring lands with hot triggers. Messages from members whose `joined_at` is more than 24h old are not recorded as "first" — the real first message must have happened while the bot was offline. |

The **Discord API deliberately hides several profile signals from bots** (connections such as Twitch or X, nameplate, display-name colour, profile widgets, current Nitro subscription state for other users). Static scoring therefore has a real ceiling; behavioural triggers and a local blocklist (see below) will close the gap for well-disguised accounts.

Stop the bot with `Ctrl+C`.

## Moderator commands

Available to any user with the `Moderate Members` (timeout) permission. This is Discord's designated moderator-tier permission and is enforced two ways:

- **Default permission** on the command — hides it from non-mods in the picker and blocks execution.
- **Code-level check** on every invocation — refuses even if the default permission is overridden in **Server Settings → Integrations → Regulus**.

Both commands reply ephemerally so the target member cannot see the response, and both are guild-only (no DM invocation).

| Trigger                                    | Effect                                                                                                         |
|--------------------------------------------|----------------------------------------------------------------------------------------------------------------|
| `/audit member:@user`                      | Runs the trust audit on `@user` and returns the audit embed ephemerally.                                       |
| Right-click a user → **Apps → Audit user** | Same as `/audit`, invoked via context menu.                                                                    |
| `/audit-id user_id:<int>`                  | Audits any Discord user by raw ID — works for banned/left users. Member-only signals (server booster, live onboarding state) return `n/a`; historical DB data still contributes. |
| `/flag member:@user reason:"..."`          | Adds the user to the blocklist. Posts a public notice + updated audit embed to the mod channel.                |
| `/unflag member:@user`                     | Deactivates all active flags for the user. Posts a public notice to the mod channel.                           |

Commands are guild-scoped and sync at startup for every server listed in `GUILDS` — they appear in Discord within a second or two of the bot connecting. Users without the required permission get a clean ephemeral refusal message.

The blocklist is **global to this bot instance** — a flag added while auditing on your test server also matches on your live server. This is intentional: mods operate one intelligence pool across every server the bot runs on.

## Repository layout

| Path              | Purpose                                                                            |
|-------------------|------------------------------------------------------------------------------------|
| `bot.py`          | Entry point, Discord client, event handlers, slash commands.                       |
| `scoring.py`      | Signal functions, `AuditContext` / `Audit` dataclasses, score → band mapping.      |
| `embeds.py`       | Discord embed formatting for the mod-audit channel.                                |
| `db.py`           | SQLite schema and CRUD for the `audits`, `flags`, and `members` tables.            |
| `invites.py`      | Per-guild invite-uses cache; identifies which invite each new joiner used.         |
| `regulus.db`      | SQLite database file (created on first run, gitignored).                           |
| `requirements.txt`| Python dependencies.                                                               |
| `.env.example`    | Template for local configuration (copy to `.env`).                                 |
| `.gitignore`      | Excludes `.env`, `*.db`, `__pycache__`, virtualenvs.                               |
| `README.md`       | This file.                                                                         |

---

## Design (planned)

The following are still to be built. As features land, bullets move out of here and into the operator sections above.

- **Enforcement.** An `ENFORCEMENT_MODE=active` env var will flip on: assigning `@Unverified` on join, holding low-band members off channels until promoted, and auto-kicking `Malicious`. `shadow` (current implicit behaviour) stays available for observation.
- **Hot triggers.** Behavioural shortcuts on `@Unverified` users — first message contains a Discord invite, image spam, mention attempts, phishing keywords — will instantly drop the user to `Malicious` regardless of static score.
- **Additional signals.** Invite used, mutual-server count, banner presence, blocklist match. All wire into the same `Audit` structure and appear in the same embed.
- **Personal invites.** Joining via a mod-issued personal invite will contribute a strong positive weight, near auto-approval.
- **Interactive buttons.** The audit embed will grow `[Approve] [Deny] [Watch]` buttons wired to bot actions.
- **More commands.** `/approve @user`, `/deny @user`, `/watchlist` mirror the buttons.
- **Blocklist by more than user ID.** Flag entries currently key on `user_id` only. Add username and avatar-hash matching so a scammer that returns under a new account with the same profile still triggers the blocklist signal.
- **Manual override.** Every automatic action will be undoable from the audit channel.
