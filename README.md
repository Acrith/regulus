# Regulus

Trust-tier moderation bot for Discord community servers. Designed to defend against scam-account raids by auditing new joiners against configurable signals, gating channel access behind an `@Unverified` role, and escalating suspicious accounts to moderator review.

**Status: shadow or active enforcement, per guild.** On member join the bot computes a trust score from twelve signals (blocklist match, invite used, mutual servers with the bot, account age, avatar, banner, avatar decoration, server-boost status, public flags, username pattern, onboarding speed, first-message timing), maps it to a band, and posts an audit embed to a configured moderator channel. Behavioural signals update as events unfold — when a member completes Onboarding and when they send their first message, the mod channel gets a notice plus an updated audit embed reflecting the new information. Every audit — automatic or manual — is persisted to a local SQLite database. Moderators can `/flag @user` to add someone to the blocklist (auto-Malicious on future audits) or `/unflag @user` to remove them. Manual audits are available via `/audit @user` or `/audit-id user_id:<int>` or the right-click **Apps → Audit user** menu. Each guild's enforcement mode is independently configurable via `/enforcement`: **`shadow`** posts audits and does nothing else; **`active`** additionally assigns `@Unverified` to joiners scoring below the configured band threshold and kicks / bans / holds Malicious joiners per config. Hold notices carry interactive `[Approve] [Deny] [Watch]` buttons; kick / ban notices carry `[Undo]`. Buttons persist across bot restarts. See [Design (planned)](#design-planned) below for what is still ahead — chiefly the hot-triggers commit that catches scam behaviour from members who are already inside.

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

Then, as behaviour unfolds, the bot **edits the original audit embed in place** (via the persisted `audit_message_id`) so a member has exactly one embed in the mod channel that always reflects their latest state. A small text notice is posted as a Discord **reply** to that embed, so mods see the delta plus a clickable jump to the current audit:

- **Onboarding completed** (either Rules Screening's `pending` transition or the `COMPLETED_ONBOARDING` member flag) → `onboarding_completed_at` is set; embed updated; notice: `<user> completed onboarding in <duration>` with a `**speedrun**` / `fast` marker for suspicious times.
- **First message sent** → `first_message_at` is set; embed updated; notice: `<user> first message in <channel>, <duration> after join` with the message preview (attachment or embed count if no text). A first message within 30 seconds of join is flagged `**immediately after join**`.
- **`/flag` and `/unflag`** also edit the same embed and reply-notice, so the mod channel history stays clean and the "current picture" is always where you left off scrolling.

Bands, from highest score to lowest: `Trusted`, `Likely-safe`, `Neutral`, `Suspicious`, `Malicious`. Thresholds are defined in `scoring.py` and are the initial guess — expect to tune them.

Current signals, defined in `scoring.py`:

| Signal              | Reads                                | Weight range       | Notes                                                                                              |
|---------------------|--------------------------------------|--------------------|----------------------------------------------------------------------------------------------------|
| Blocklist           | local SQLite `flags` table           | 0 or −10           | Overrides everything: any active flag drops the user to Malicious. Row shows every guild the user is currently flagged in, each with actor + date + reason. |
| Invite              | per-guild invite cache               | 0 (informational)  | Which invite was used, by whom, use count. `unknown` on vanity URL, cold cache, or `/audit` runs.  |
| Mutual servers      | `member.mutual_guilds` + `bot.guilds` | 0 (informational)  | Shared server count with the bot (excluding the current guild). Currently unweighted — penalising 0 mutuals only makes sense once the bot is in several sister communities. Re-enable a negative weight for 0 mutuals when the bot is running on 4+ sister servers. |
| Account age         | `member.created_at`                  | −3 to +2           | Days since Discord account creation.                                                               |
| Server tenure       | `members.joined_at`                  | 0 to +3            | How long they've been a member of this specific guild. <30d = 0. <6mo = +1. <1y = +2. 1y+ = +3. Fights against a false-positive Blocklist hit for long-time members whose accounts were hijacked — the audit shows both signals side by side. |
| Avatar              | `member.avatar` + `is_animated()`    | −2, +1, or +2      | Default: −2. Static custom: +1. Animated (Nitro-only): +2.                                         |
| Banner              | `full_user.banner`                   | 0 or +1            | Custom banner requires Nitro; weak positive.                                                       |
| Avatar decoration   | `member.avatar_decoration`           | 0 or +1            | Overlay around the avatar; Nitro-only.                                                             |
| Server booster      | `member.premium_since`               | 0 or +3            | Boosting the current guild — strong positive.                                                      |
| Public flags        | `member.public_flags`                | −1 to +3           | HypeSquad / Nitro Early / Active Developer / etc. Limited set exposed by the API.                 |
| Username pattern    | regex on `member.name`               | −2 or 0            | Trailing `\d{4,}$` — the `word####` scam signature.                                                |
| Onboarding speed    | `members.onboarding_completed_at`    | −3 to +1           | Seconds between join and onboarding completion. <7s: speedrun. <15s: fast. 15s–30m: normal. >30m: deliberate. If the member state shows completion but the bot missed the event (was down), the signal reports so honestly at weight 0. |
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
| `/flag member:@user reason:"..."`          | Adds the user to the blocklist. Posts a local notice with `[Ban] [Kick] [Hold] [Ignore]` buttons in the mod channel, and dispatches the same alert to every other guild where the user is a member AND enforcement is `active`. Buttons act only in the guild the notice was posted in. |
| `/unflag member:@user`                     | Deactivates all active flags for the user. Posts a public notice to the mod channel.                           |
| `/audit-guild-blocklist`                   | Scans the current guild's member list against active flags and returns an ephemeral list of matches, sorted by flag count. Fast — no Discord API calls per member, just DB lookups. |
| `/purge-flagged action:<dry_run\|execute>`  | Applies the guild's `malicious_action` to every currently-in-guild flagged member. `dry_run` lists targets; `execute` performs the actions. Requires `Manage Guild` and refuses to execute in `shadow` mode. |
| `/enforcement show`                        | Displays current per-guild enforcement configuration.                                                          |
| `/enforcement mode new_mode:<shadow\|active>` | Flip the guild's enforcement mode. `shadow` = observe only. `active` = act on join per band.                   |
| `/enforcement hold_below band:<band>`      | Bands strictly worse than this are held on `@Unverified` when enforcement is active.                           |
| `/enforcement malicious action:<kick\|ban\|hold>` | What to do with joiners scored Malicious. Bans also auto-flag so a rejoin under the same ID lands Malicious. |
| `/setup-unverified role_name:<str> open_channels:<str>` | **Server-owner tier.** Creates the `@Unverified` role and applies deny overrides on every channel. `open_channels` is a comma-separated list of channel names that remain viewable (e.g. `welcome,rules`). Requires `Manage Guild`. Idempotent — safe to re-run when adding new channels. |

Commands are guild-scoped and sync at startup for every server listed in `GUILDS` — they appear in Discord within a second or two of the bot connecting. Users without the required permission get a clean ephemeral refusal message.

The blocklist is **global to this bot instance** — a flag added while auditing on your test server also matches on your live server. This is intentional: mods operate one intelligence pool across every server the bot runs on.

## Repository layout

| Path              | Purpose                                                                            |
|-------------------|------------------------------------------------------------------------------------|
| `bot.py`          | Entry point, Discord client, event handlers, slash commands.                       |
| `scoring.py`      | Signal functions, `AuditContext` / `Audit` dataclasses, score → band mapping.      |
| `embeds.py`       | Discord embed formatting for the mod-audit channel.                                |
| `db.py`           | SQLite schema and CRUD for the `audits`, `flags`, `members`, and `guild_config` tables. |
| `enforcement.py`  | Enforcement whitelisted values (modes, band ordering, malicious actions). Dispatch logic will land here in a subsequent commit. |
| `invites.py`      | Per-guild invite-uses cache; identifies which invite each new joiner used.         |
| `regulus.db`      | SQLite database file (created on first run, gitignored).                           |
| `requirements.txt`| Python dependencies.                                                               |
| `.env.example`    | Template for local configuration (copy to `.env`).                                 |
| `.gitignore`      | Excludes `.env`, `*.db`, `__pycache__`, virtualenvs.                               |
| `README.md`       | This file.                                                                         |

---

## Design (planned)

The following are still to be built. As features land, bullets move out of here and into the operator sections above.

- **Hot triggers.** Behavioural shortcuts on any member — Discord invite in message, phishing URL patterns, attachment + role-mention combo, cross-post spam across channels within 30 seconds. Tiered by tenure (auto-action within first 7 days, alert-with-buttons for older members). Hard triggers can `[Ban+Purge]` in one click; `[Undo]` reverses everything.
- **More commands.** `/approve @user`, `/deny @user`, `/watchlist` mirror the buttons (button clicks work today; slash-command mirrors are for cases where the notice has scrolled out of view).
- **Blocklist by more than user ID.** Flag entries currently key on `user_id` only. Add username and avatar-hash matching so a scammer returning under a new account with the same profile still triggers the blocklist signal.

## Enforcement

Enforcement is **off by default**. Every guild starts in `shadow` mode where the bot only observes and records — no roles are assigned, no members are kicked, no messages are deleted. The `active` mode requires **two explicit operator actions**:

1. Run `/setup-unverified` — creates the `@Unverified` role and applies deny overrides on every channel. Requires the bot to have `Manage Roles` and `Manage Channels`.
2. Run `/enforcement mode new_mode:active` — flips the guild's config in the `guild_config` table.

Neither step affects any guild the bot is a member of but is not in `GUILDS`. Guilds hosting the bot with `permissions=0` (no elevated permissions) cannot be affected by enforcement regardless of code state — Discord enforces permissions server-side, so a bot without `Manage Roles` cannot assign roles even if it tried to.

### What `active` mode actually does

Each new join runs the trust audit. Based on the result and the guild's config:

| Band            | Config-controlled action                                                             |
|-----------------|--------------------------------------------------------------------------------------|
| Trusted         | No action.                                                                            |
| Likely-safe     | No action (with default `hold_below_band=Likely-safe`).                              |
| Neutral         | Assign `@Unverified`. Notice + Approve / Deny / Watch buttons.                       |
| Suspicious      | Assign `@Unverified`. Notice + buttons.                                              |
| Malicious       | `malicious_action` decides: `kick`, `ban` (also auto-flags), or `hold`.              |

Interactive buttons on the notice:

- **Approve** (on hold) — removes `@Unverified`. Strikes through the notice.
- **Deny** (on hold) — kicks + auto-flags. Strikes through the notice.
- **Watch** (on hold) — keeps `@Unverified` in place, records the mod's decision. (Behavioural escalation for watched members is coming with the hot-triggers commit.)
- **Undo ban** (on ban) — lifts the ban and clears the auto-flag so the user can rejoin cleanly.
- **Undo** (on kick) — clears any auto-flags. The user can rejoin freely; kicks are not persistent.

Buttons use `discord.ui.DynamicItem` with template-matched `custom_id`s (`regulus:hold_approve:USER:GUILD` etc.), registered at startup. They survive bot restarts — you don't lose the ability to Approve a held user just because Regulus was restarted between join and click.

Any button click also refuses users who lack `Moderate Members` in the guild, so a plain member who somehow sees the notice cannot trigger enforcement actions on other members.

### Configuration table

`guild_config` is a per-guild row containing:

| Column               | Default        | Meaning                                                                                   |
|----------------------|----------------|-------------------------------------------------------------------------------------------|
| `mode`               | `shadow`       | `shadow` = audits only. `active` = enforcement dispatch (once wired) will consume config. |
| `unverified_role_id` | `NULL`         | Set by `/setup-unverified`. `active` mode refuses to enable while this is null.           |
| `hold_below_band`    | `Likely-safe`  | Bands strictly worse than this get held on `@Unverified` in `active` mode.                |
| `malicious_action`   | `kick`         | What to do with Malicious-band joiners: `kick`, `ban`, or `hold`.                         |
| `updated_at`         | now            | ISO timestamp of last change.                                                             |
| `updated_by`         | `NULL`         | User ID of the mod who made the last change.                                              |

Rows are created lazily when a guild is first read via `get_guild_config`. Every change made via `/enforcement` posts a notice to the guild's mod channel with the actor and the change, providing an audit trail.

### Cross-server threat alerts

When any moderator runs `/flag` in any guild, Regulus dispatches a **threat alert** to every other guild where:

1. The user is currently a member,
2. Enforcement mode is `active`, and
3. The guild is listed in `GUILDS`.

Each alert is a plain-text notice in that guild's mod channel with `[Ban] [Kick] [Hold] [Ignore]` buttons. **Buttons act only in the guild the notice was posted in** — a mod in Server B clicking `[Ban]` bans the user from Server B only. Server A's mods (who originated the flag) get their own local notice with the same button set for the same reason. Every mod team decides for their own guild.

This preserves each guild's autonomy while sharing intelligence at the speed of a single command. If a hijacked account is caught in one server, allied servers get the alert within seconds and can each independently take the local action they judge appropriate.

Alerts do not run a fresh audit in each notified guild (that would be expensive). Mods can run `/audit member:@user` locally if they want the full picture before clicking a button.

If a guild is in `shadow` mode, it receives no alert at all — shadow mode is a **defence-in-depth** posture that opts out of both automatic enforcement AND cross-guild reactive alerts.

### Native-ban mirroring

Regulus listens to `on_member_ban`. When a moderator bans a user via Discord's native right-click UI (rather than via Regulus's own enforcement path), the bot peeks the audit log to identify the actor and reason, then:

- Adds a `flags` row with `reason: "native ban: <mod's reason>"` and the mod as `flagged_by`.
- Posts a plain-text notice to the mod channel so the record is human-visible too.

If Regulus itself performed the ban (via `active` mode or the `[Ban]` button on an alert), the listener recognises its own user ID in the audit log and skips — no double-flag. Requires the bot to have `View Audit Log` permission; if absent, the listener logs a warning and doesn't crash.

### For inspecting operators

The set of actions the bot can take is bounded strictly by:

1. The permissions granted to it in Discord (a bot with `permissions=0` can literally not do anything besides receive events),
2. The `GUILDS` env var (any guild not listed there is silently ignored — see the early return in `on_member_join`, `on_member_update`, and `on_message`),
3. The `guild_config.mode` value (dispatch will refuse to act in `shadow`),
4. The `unverified_role_id` being set (`active` mode refuses to enable without it).

There is no code path that lets the bot act in a guild that is either not listed in `GUILDS` or configured as `shadow`. This is the deliberate safe-by-default posture.
- **Manual override.** Every automatic action will be undoable from the audit channel.
