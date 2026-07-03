import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import db
import enforcement
import invites
from embeds import build_audit_embed
from scoring import Audit, audit, audit_by_user_id, fmt_duration

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")


def _parse_guilds(raw: str) -> dict[int, int]:
    if not raw or not raw.strip():
        raise SystemExit(
            "GUILDS is empty; expected 'guild_id:mod_channel_id' pairs, comma-separated"
        )
    result: dict[int, int] = {}
    for i, pair in enumerate(raw.split(","), start=1):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise SystemExit(
                f"GUILDS entry #{i} is malformed: '{pair}' "
                "(expected 'guild_id:mod_channel_id')"
            )
        guild_part, channel_part = pair.split(":", 1)
        try:
            guild_id = int(guild_part.strip())
            channel_id = int(channel_part.strip())
        except ValueError:
            raise SystemExit(
                f"GUILDS entry #{i}: guild_id and mod_channel_id must be integers "
                f"(got '{pair}')"
            )
        result[guild_id] = channel_id
    if not result:
        raise SystemExit("GUILDS contained no valid pairs after parsing")
    return result


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
log = logging.getLogger("regulus")

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN missing from .env")
GUILDS: dict[int, int] = _parse_guilds(os.getenv("GUILDS", ""))
log.info("configured guilds (%d): %s",
         len(GUILDS), ", ".join(str(g) for g in GUILDS))

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _elapsed_since(iso_ts: str) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso_ts)).total_seconds()


async def _record_audit(user_id: int, guild_id: int, result: Audit, kind: str) -> None:
    signals_data = [
        {"name": s.name, "detail": s.detail, "weight": s.weight}
        for s in result.signals
    ]
    await db.record_audit(
        user_id=user_id,
        guild_id=guild_id,
        kind=kind,
        score=result.score,
        band=result.band,
        signals_json=json.dumps(signals_data),
    )


async def _post_or_update_audit(
    user_id: int,
    guild_id: int,
    embed: discord.Embed,
) -> Optional[discord.Message]:
    """Edit the member's existing mod-channel audit message with the new
    embed. If it doesn't exist or the edit fails, post a fresh message
    and remember its ID for future edits."""
    record = await db.get_member_record(user_id, guild_id)
    if (record is not None
            and record.audit_channel_id is not None
            and record.audit_message_id is not None):
        channel = bot.get_channel(record.audit_channel_id)
        if channel is not None:
            try:
                msg = await channel.fetch_message(record.audit_message_id)
                await msg.edit(embed=embed)
                return msg
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass  # fall through to fresh post

    mod_channel_id = GUILDS.get(guild_id)
    if mod_channel_id is None:
        return None
    channel = bot.get_channel(mod_channel_id)
    if channel is None:
        log.warning("mod channel %s unresolved for guild %s", mod_channel_id, guild_id)
        return None
    try:
        msg = await channel.send(embed=embed)
    except discord.Forbidden:
        log.error("cannot post audit: missing permission in #%s", channel.name)
        return None
    except discord.HTTPException as e:
        log.error("failed to post audit embed: %s", e)
        return None

    await db.set_audit_message(user_id, guild_id, mod_channel_id, msg.id)
    return msg


async def _post_notice(
    guild_id: int,
    content: str,
    reply_to_message_id: Optional[int] = None,
    view: Optional[discord.ui.View] = None,
) -> Optional[discord.Message]:
    """Post a plain-text notice to the guild's mod channel. If a message
    ID is given, post it as a Discord reply so mods can click through to
    the referenced audit embed. A view attaches interactive buttons."""
    mod_channel_id = GUILDS.get(guild_id)
    if mod_channel_id is None:
        return None
    channel = bot.get_channel(mod_channel_id)
    if channel is None:
        return None
    reference = None
    if reply_to_message_id is not None:
        reference = discord.MessageReference(
            message_id=reply_to_message_id,
            channel_id=mod_channel_id,
            fail_if_not_exists=False,
        )
    kwargs: dict[str, object] = {"content": content, "reference": reference}
    if view is not None:
        kwargs["view"] = view
    try:
        return await channel.send(**kwargs)
    except discord.Forbidden:
        log.error("cannot post notice: missing permission in #%s", channel.name)
    except discord.HTTPException as e:
        log.error("failed to post notice: %s", e)
    return None


async def _enforce_join(
    member: discord.Member,
    result: Audit,
    updated_msg: Optional[discord.Message],
) -> None:
    """Consume the guild's enforcement config and act on the audit band."""
    config = await db.get_guild_config(member.guild.id)
    action = enforcement.decide_action(result.band, config)
    if action is enforcement.Action.NONE:
        return

    reply_id = updated_msg.id if updated_msg is not None else None

    if action is enforcement.Action.HOLD:
        role_id = config.unverified_role_id
        if role_id is None:
            log.error("cannot hold %s: no Unverified role configured", member.id)
            return
        role = member.guild.get_role(role_id)
        if role is None:
            log.error("cannot hold %s: Unverified role %s not found", member.id, role_id)
            return
        try:
            await member.add_roles(role, reason=f"Regulus enforcement: band={result.band}")
        except discord.Forbidden:
            log.error("cannot hold %s: missing Manage Roles or role hierarchy", member.id)
            await _post_notice(
                member.guild.id,
                f"Wanted to assign {role.mention} to {member.mention} "
                f"(band `{result.band}`) but I don't have permission. "
                f"Check my Manage Roles perm and that my role is above `{role.name}`.",
                reply_to_message_id=reply_id,
            )
            return
        except discord.HTTPException as e:
            log.error("cannot hold %s: %s", member.id, e)
            return
        view = enforcement.hold_view(member.id, member.guild.id)
        await _post_notice(
            member.guild.id,
            f"{member.mention} assigned {role.mention} — band `{result.band}`. Choose:",
            reply_to_message_id=reply_id,
            view=view,
        )
        return

    if action is enforcement.Action.KICK:
        try:
            await member.kick(reason=f"Regulus enforcement: band={result.band}")
        except discord.Forbidden:
            log.error("cannot kick %s: missing Kick Members", member.id)
            await _post_notice(
                member.guild.id,
                f"Wanted to kick {member.mention} (band `{result.band}`) but I "
                "don't have the Kick Members permission.",
                reply_to_message_id=reply_id,
            )
            return
        except discord.HTTPException as e:
            log.error("cannot kick %s: %s", member.id, e)
            return
        view = enforcement.undo_view(member.id, member.guild.id, enforcement.Action.KICK)
        await _post_notice(
            member.guild.id,
            f"Auto-kicked **{member}** (`{member.id}`) — band `{result.band}`.",
            reply_to_message_id=reply_id,
            view=view,
        )
        return

    if action is enforcement.Action.BAN:
        try:
            await member.ban(
                reason=f"Regulus enforcement: band={result.band}",
                delete_message_seconds=86400,
            )
        except discord.Forbidden:
            log.error("cannot ban %s: missing Ban Members", member.id)
            await _post_notice(
                member.guild.id,
                f"Wanted to ban {member.mention} (band `{result.band}`) but I "
                "don't have the Ban Members permission.",
                reply_to_message_id=reply_id,
            )
            return
        except discord.HTTPException as e:
            log.error("cannot ban %s: %s", member.id, e)
            return
        await db.add_flag(
            user_id=member.id,
            guild_id=member.guild.id,
            flagged_by=bot.user.id if bot.user else 0,
            reason=f"auto-enforcement ban: band={result.band}",
        )
        view = enforcement.undo_view(member.id, member.guild.id, enforcement.Action.BAN)
        await _post_notice(
            member.guild.id,
            f"Auto-banned **{member}** (`{member.id}`) — band `{result.band}`. "
            "Last 24h of their messages deleted; auto-flagged for future audits.",
            reply_to_message_id=reply_id,
            view=view,
        )
        return


async def _bootstrap_members(guild: discord.Guild) -> None:
    created = 0
    for member in guild.members:
        if member.bot:
            continue
        existing = await db.get_member_record(member.id, guild.id)
        if existing is not None:
            continue
        joined = member.joined_at or datetime.now(timezone.utc)
        await db.upsert_member_join(
            member.id, guild.id,
            joined.isoformat(timespec="seconds"),
            invite_code=None,
        )
        created += 1
    if created:
        log.info("bootstrapped %d member record(s) for guild %s", created, guild.id)


@bot.event
async def setup_hook():
    await db.init()
    log.info("database initialised at %s", db.DB_PATH)
    for item_cls in enforcement.ALL_DYNAMIC_ITEMS:
        bot.add_dynamic_items(item_cls)
    log.info("registered %d persistent button item(s)", len(enforcement.ALL_DYNAMIC_ITEMS))
    for guild_id in GUILDS:
        guild = discord.Object(id=guild_id)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        log.info("synced %d slash command(s) to guild %s", len(synced), guild_id)


@bot.event
async def on_ready():
    log.info("logged in as %s (id=%s)", bot.user, bot.user.id)
    for guild in bot.guilds:
        marker = "" if guild.id in GUILDS else "  (not configured — events ignored)"
        log.info("  guild: %s (id=%s, members=%s)%s",
                 guild.name, guild.id, guild.member_count, marker)
        if guild.id in GUILDS:
            channel = bot.get_channel(GUILDS[guild.id])
            if channel is None:
                log.warning("    mod channel %s not visible in this guild",
                            GUILDS[guild.id])
            else:
                log.info("    mod channel: #%s (id=%s)", channel.name, channel.id)
            await invites.refresh(guild)
            await _bootstrap_members(guild)
            config = await db.get_guild_config(guild.id)
            log.info("    enforcement: mode=%s hold_below=%s malicious=%s role_id=%s",
                     config.mode, config.hold_below_band,
                     config.malicious_action,
                     config.unverified_role_id or "unset")


@bot.event
async def on_guild_join(guild: discord.Guild):
    log.info("joined new guild: %s (id=%s)", guild.name, guild.id)
    if guild.id in GUILDS:
        await invites.refresh(guild)


@bot.event
async def on_invite_create(invite: discord.Invite):
    invites.note_created(invite)


@bot.event
async def on_invite_delete(invite: discord.Invite):
    invites.note_deleted(invite)


@bot.event
async def on_member_ban(guild: discord.Guild, user: Union[discord.User, discord.Member]):
    """When any mod (not the bot) bans a user via Discord's native UI, mirror
    that into the blocklist so the intel survives and later audits see it."""
    if guild.id not in GUILDS:
        return

    actor_id: Optional[int] = None
    reason: Optional[str] = None
    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.ban, limit=5):
            if entry.target is not None and entry.target.id == user.id:
                actor_id = entry.user.id if entry.user is not None else None
                reason = entry.reason
                break
    except discord.Forbidden:
        log.warning("cannot peek audit log for ban in guild %s (need View Audit Log)",
                    guild.id)
        return
    except discord.HTTPException as e:
        log.warning("audit log fetch failed for guild %s: %s", guild.id, e)
        return

    if actor_id is None:
        return  # ban with no traceable audit-log entry
    if bot.user is not None and actor_id == bot.user.id:
        return  # our own ban path already flagged

    reason_text = (reason or "native Discord ban").strip()
    if len(reason_text) > 180:
        reason_text = reason_text[:177] + "..."

    await db.add_flag(
        user_id=user.id,
        guild_id=guild.id,
        flagged_by=actor_id,
        reason=f"native ban: {reason_text}",
    )
    await _post_notice(
        guild.id,
        f"<@{actor_id}> banned <@{user.id}> (`{user.id}`) via Discord — "
        f"auto-added to blocklist.\nReason: {reason_text}",
    )


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return
    if member.guild.id not in GUILDS:
        return

    used_invite = await invites.find_used(member.guild)
    joined_at = (member.joined_at or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    invite_code = used_invite.code if used_invite else None
    inviter_id = used_invite.inviter.id if used_invite and used_invite.inviter else None
    inviter_name = used_invite.inviter.name if used_invite and used_invite.inviter else None
    await db.upsert_member_join(
        member.id, member.guild.id, joined_at,
        invite_code=invite_code,
        invite_inviter_id=inviter_id,
        invite_inviter_name=inviter_name,
    )

    result = await audit(member, bot, used_invite=used_invite)
    await _record_audit(member.id, member.guild.id, result, "join")
    log.info("member joined: %s (id=%s, guild=%s, score=%+d, band=%s, invite=%s)",
             member, member.id, member.guild.id, result.score, result.band,
             invite_code or "unknown")

    embed = build_audit_embed(member, result)
    updated = await _post_or_update_audit(member.id, member.guild.id, embed)
    await _enforce_join(member, result, updated)


def _detect_onboarding_completed(before: discord.Member, after: discord.Member) -> bool:
    if before.pending and not after.pending:
        return True
    before_done = getattr(before.flags, "completed_onboarding", False)
    after_done = getattr(after.flags, "completed_onboarding", False)
    if after_done and not before_done:
        return True
    return False


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if after.bot:
        return
    if after.guild.id not in GUILDS:
        return
    if not _detect_onboarding_completed(before, after):
        return

    now_iso = _now_iso()
    set_ok = await db.set_onboarding_completed(after.id, after.guild.id, now_iso)
    if not set_ok:
        return

    record = await db.get_member_record(after.id, after.guild.id)
    elapsed_str = "unknown"
    note = ""
    if record is not None:
        elapsed = _elapsed_since(record.joined_at)
        elapsed_str = fmt_duration(elapsed)
        if elapsed < 5:
            note = " — **speedrun**"
        elif elapsed < 30:
            note = " — fast"

    result = await audit(after, bot)
    await _record_audit(after.id, after.guild.id, result, "onboarding")
    embed = build_audit_embed(after, result)
    updated = await _post_or_update_audit(after.id, after.guild.id, embed)
    await _post_notice(
        after.guild.id,
        f"{after.mention} completed onboarding in {elapsed_str}{note}",
        reply_to_message_id=updated.id if updated else None,
    )


_FIRST_MSG_RECORD_WINDOW_SECONDS = 24 * 3600


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    if message.guild.id not in GUILDS:
        return

    record = await db.get_member_record(message.author.id, message.guild.id)
    if record is None or record.first_message_at is not None:
        return
    joined_ago = _elapsed_since(record.joined_at)
    if joined_ago > _FIRST_MSG_RECORD_WINDOW_SECONDS:
        return

    now_iso = _now_iso()
    set_ok = await db.set_first_message(message.author.id, message.guild.id, now_iso)
    if not set_ok:
        return

    elapsed = _elapsed_since(record.joined_at)
    note = " — **immediately after join**" if elapsed < 30 else ""
    preview = message.content.strip().replace("\n", " ")
    if len(preview) > 200:
        preview = preview[:197] + "..."
    if not preview and message.attachments:
        preview = f"({len(message.attachments)} attachment(s), no text)"
    elif not preview and message.embeds:
        preview = f"({len(message.embeds)} embed(s), no text)"
    elif not preview:
        preview = "(empty)"

    member = message.guild.get_member(message.author.id) or await message.guild.fetch_member(message.author.id)
    result = await audit(member, bot)
    await _record_audit(member.id, member.guild.id, result, "first_message")
    embed = build_audit_embed(member, result)
    updated = await _post_or_update_audit(member.id, member.guild.id, embed)
    await _post_notice(
        message.guild.id,
        f"{message.author.mention} first message in {message.channel.mention}, "
        f"{fmt_duration(elapsed)} after join{note}\n> {preview}",
        reply_to_message_id=updated.id if updated else None,
    )


async def _reply_with_audit(interaction: discord.Interaction, member: discord.Member) -> None:
    await interaction.response.defer(ephemeral=True)
    result = await audit(member, bot)
    await _record_audit(member.id, member.guild.id, result, "manual")
    embed = build_audit_embed(member, result)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="audit",
    description="Run the trust audit on a member and show the result.",
)
@app_commands.describe(member="The member to audit")
@app_commands.default_permissions(moderate_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def audit_command(interaction: discord.Interaction, member: discord.Member) -> None:
    await _reply_with_audit(interaction, member)


@bot.tree.context_menu(name="Audit user")
@app_commands.default_permissions(moderate_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def audit_context_menu(interaction: discord.Interaction, member: discord.Member) -> None:
    await _reply_with_audit(interaction, member)


@bot.tree.command(
    name="audit-id",
    description="Run the trust audit on any user by ID (works for banned/left users).",
)
@app_commands.describe(
    user_id="User ID to audit. Right-click a message or user in Discord, then Copy User ID."
)
@app_commands.default_permissions(moderate_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def audit_id_command(interaction: discord.Interaction, user_id: str) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        parsed_id = int(user_id.strip())
    except ValueError:
        await interaction.followup.send(
            f"`{user_id}` is not a valid user ID — expected a number.",
            ephemeral=True,
        )
        return

    result = await audit_by_user_id(parsed_id, interaction.guild.id, bot)
    if result is None:
        await interaction.followup.send(
            f"No Discord user found with ID `{parsed_id}`.",
            ephemeral=True,
        )
        return

    try:
        user = await bot.fetch_user(parsed_id)
    except discord.HTTPException:
        await interaction.followup.send(
            "Could not fetch user data for the embed.",
            ephemeral=True,
        )
        return

    await _record_audit(parsed_id, interaction.guild.id, result, "manual-id")
    embed = build_audit_embed(user, result)
    await interaction.followup.send(embed=embed, ephemeral=True)


async def _dispatch_cross_guild_alerts(
    user_id: int,
    flagged_by_id: int,
    reason: Optional[str],
    originating_guild_id: int,
) -> int:
    """Post a threat-alert notice with local action buttons in every guild
    where (a) the user is currently a member, (b) enforcement is active,
    and (c) the guild is not the one where the flag originated. Returns
    the count of alerts posted."""
    originating_guild = bot.get_guild(originating_guild_id)
    origin_name = (
        originating_guild.name if originating_guild is not None
        else f"guild `{originating_guild_id}`"
    )
    reason_line = f" — reason: *{reason}*" if reason else ""
    posted = 0

    for guild in bot.guilds:
        if guild.id not in GUILDS:
            continue
        if guild.id == originating_guild_id:
            continue
        member = guild.get_member(user_id)
        if member is None:
            continue
        config = await db.get_guild_config(guild.id)
        if config.mode != "active":
            continue

        content = (
            f"**Cross-server threat alert**\n"
            f"<@{user_id}> (`{user_id}`) was just flagged in **{origin_name}** "
            f"by <@{flagged_by_id}>{reason_line}.\n"
            f"They are currently a member of this server. "
            f"Run `/audit member:<@{user_id}>` for local context.\n"
            f"Choose:"
        )
        view = enforcement.cross_guild_alert_view(user_id, guild.id)
        result = await _post_notice(guild.id, content, view=view)
        if result is not None:
            posted += 1
    return posted


@bot.tree.command(
    name="flag",
    description="Add a user to the blocklist. Future audits mark them Malicious.",
)
@app_commands.describe(
    member="The member to flag",
    reason="Why they are being flagged (shown in future audits)",
)
@app_commands.default_permissions(moderate_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def flag_command(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "",
) -> None:
    await interaction.response.defer(ephemeral=True)
    reason_text = reason.strip() or None
    await db.add_flag(
        user_id=member.id,
        guild_id=interaction.guild.id,
        flagged_by=interaction.user.id,
        reason=reason_text,
    )
    result = await audit(member, bot)
    await _record_audit(member.id, member.guild.id, result, "flag")
    embed = build_audit_embed(member, result)
    updated = await _post_or_update_audit(member.id, member.guild.id, embed)

    local_content = (
        f"{interaction.user.mention} flagged {member.mention}"
        + (f" — {reason_text}" if reason_text else "")
        + "\nChoose:"
    )
    local_view = enforcement.cross_guild_alert_view(member.id, interaction.guild.id)
    await _post_notice(
        interaction.guild.id,
        local_content,
        reply_to_message_id=updated.id if updated else None,
        view=local_view,
    )

    alert_count = await _dispatch_cross_guild_alerts(
        user_id=member.id,
        flagged_by_id=interaction.user.id,
        reason=reason_text,
        originating_guild_id=interaction.guild.id,
    )

    summary = f"Added **{member}** to the blocklist."
    if alert_count > 0:
        summary += (
            f" Posted **{alert_count}** cross-server alert(s) in other "
            "guilds where they're currently a member."
        )
    await interaction.followup.send(summary, ephemeral=True)


@bot.tree.command(
    name="unflag",
    description="Remove a user from the blocklist.",
)
@app_commands.describe(member="The member to unflag")
@app_commands.default_permissions(moderate_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def unflag_command(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    await interaction.response.defer(ephemeral=True)
    n = await db.deactivate_flags(member.id)
    if n == 0:
        await interaction.followup.send(
            f"**{member}** had no active flags.",
            ephemeral=True,
        )
        return
    result = await audit(member, bot)
    await _record_audit(member.id, member.guild.id, result, "unflag")
    embed = build_audit_embed(member, result)
    updated = await _post_or_update_audit(member.id, member.guild.id, embed)
    await _post_notice(
        interaction.guild.id,
        f"{interaction.user.mention} removed {member.mention} from the blocklist "
        f"({n} flag(s) deactivated).",
        reply_to_message_id=updated.id if updated else None,
    )
    await interaction.followup.send(
        f"Removed **{member}** from the blocklist.",
        ephemeral=True,
    )


_UNVERIFIED_DENY_KEYS = (
    "send_messages", "send_messages_in_threads",
    "create_public_threads", "create_private_threads",
    "add_reactions", "attach_files", "embed_links",
    "mention_everyone", "use_application_commands",
)

_CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")


def _resolve_role_reference(
    raw: str,
    guild: discord.Guild,
) -> tuple[Optional[discord.Role], str]:
    """Resolve a user-supplied role reference to (existing Role or None, name).

    Accepts:
      - A role mention (`<@&ID>`), auto-completed by the slash-command UI
        when the operator types `@`. Looked up by ID.
      - A plain name (`Unverified`), optionally with a leading `@`.

    If a mention resolves to a real role in the guild, returns that role
    and its current name. If it resolves to a role that no longer exists,
    returns (None, "") to signal the caller should error out. If the input
    is a plain name, returns any existing role with that name (or None to
    signal "create it").
    """
    ref = raw.strip()
    mention_match = _ROLE_MENTION_RE.fullmatch(ref)
    if mention_match is not None:
        role_id = int(mention_match.group(1))
        role = guild.get_role(role_id)
        if role is None:
            return None, ""
        return role, role.name
    name = ref.lstrip("@")
    return discord.utils.get(guild.roles, name=name), name


def _parse_open_channels(
    raw: str,
    guild: discord.Guild,
) -> tuple[set[int], set[str]]:
    """Split the open-channels input into precise IDs and fuzzy names.

    Mentions (`<#1234567890>`) match by channel ID exactly — chosen
    deliberately from the slash-command UI, no accidental collisions.
    Plain text (`welcome`, `#welcome`) matches by lowercased name and
    can therefore hit multiple channels of the same name across different
    types (e.g. text and voice both named 'general') — that's the price
    of a name-based match and is documented on the parameter.
    """
    ids: set[int] = set()
    names: set[str] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        m = _CHANNEL_MENTION_RE.fullmatch(token)
        if m is not None:
            ids.add(int(m.group(1)))
        else:
            names.add(token.lower().lstrip("#"))
    return ids, names


def _make_unverified_overwrite(can_view: bool, is_voice: bool) -> discord.PermissionOverwrite:
    overwrite = discord.PermissionOverwrite()
    overwrite.view_channel = can_view
    for perm in _UNVERIFIED_DENY_KEYS:
        setattr(overwrite, perm, False)
    if is_voice:
        # Unverified is a held state — always deny voice actions, even if
        # the channel was placed on the open list (view-only sidebar
        # presence is fine, joining a voice call is not).
        overwrite.speak = False
        overwrite.stream = False
        overwrite.connect = False
    return overwrite


@bot.tree.command(
    name="setup-unverified",
    description="Create the @Unverified role and apply deny overrides guild-wide.",
)
@app_commands.describe(
    role_name="Role to use. New name (e.g. 'Unverified') or an existing role's @mention.",
    open_channels="Channels Unverified can still view (comma-separated). Names or #mentions both work.",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_unverified_command(
    interaction: discord.Interaction,
    role_name: str = "Unverified",
    open_channels: str = "",
) -> None:
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    me = guild.me

    if not me.guild_permissions.manage_roles:
        await interaction.followup.send(
            "I need the **Manage Roles** permission to run this. Please grant it and re-run.",
            ephemeral=True,
        )
        return
    if not me.guild_permissions.manage_channels:
        await interaction.followup.send(
            "I need the **Manage Channels** permission to run this. Please grant it and re-run.",
            ephemeral=True,
        )
        return

    open_ids, open_names = _parse_open_channels(open_channels, guild)

    role, resolved_role_name = _resolve_role_reference(role_name, guild)
    if _ROLE_MENTION_RE.fullmatch(role_name.strip()) is not None and role is None:
        await interaction.followup.send(
            "The role you mentioned does not exist in this guild.",
            ephemeral=True,
        )
        return
    created = False
    if role is None:
        try:
            role = await guild.create_role(
                name=resolved_role_name,
                color=discord.Color.from_rgb(180, 90, 90),
                hoist=False,
                mentionable=False,
                reason=f"Regulus /setup-unverified by {interaction.user}",
            )
            created = True
        except discord.Forbidden:
            await interaction.followup.send(
                "Failed to create the role — I don't have permission.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"Failed to create the role: `{e}`",
                ephemeral=True,
            )
            return

    bot_top_role = me.top_role
    target_position = bot_top_role.position - 1
    if target_position > 0 and role.position != target_position:
        try:
            await role.edit(
                position=target_position,
                reason="Regulus: position Unverified below the bot's role",
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    channels_updated = 0
    categories_updated = 0
    opened_names: list[str] = []
    skipped: list[str] = []
    for channel in guild.channels:
        can_view = (
            channel.id in open_ids
            or channel.name.lower() in open_names
        )
        is_voice = isinstance(channel, (discord.VoiceChannel, discord.StageChannel))
        overwrite = _make_unverified_overwrite(can_view=can_view, is_voice=is_voice)
        try:
            await channel.set_permissions(
                role, overwrite=overwrite,
                reason=f"Regulus /setup-unverified by {interaction.user}",
            )
            if isinstance(channel, discord.CategoryChannel):
                categories_updated += 1
            else:
                channels_updated += 1
                if can_view:
                    opened_names.append(channel.name)
        except discord.Forbidden:
            skipped.append(f"#{channel.name} (missing permission)")
        except discord.HTTPException as e:
            skipped.append(f"#{channel.name} ({e})")

    await db.update_guild_config(
        guild.id,
        updated_by=interaction.user.id,
        unverified_role_id=role.id,
    )

    cat_word = "category" if categories_updated == 1 else "categories"
    lines = [
        f"**@{role.name}** {'created' if created else 'updated'} — role ID `{role.id}`",
        f"Applied deny overrides to **{channels_updated}** channel(s) "
        f"and **{categories_updated}** {cat_word}. "
        f"(Category overrides also propagate to any current or future child "
        f"channels that don't have their own override.)",
    ]
    if opened_names:
        lines.append("Left viewable: " + ", ".join(f"#{n}" for n in opened_names))
    if skipped:
        lines.append(f"Skipped **{len(skipped)}** channel(s):")
        for s in skipped[:10]:
            lines.append(f"  • {s}")
        if len(skipped) > 10:
            lines.append(f"  • …and {len(skipped) - 10} more")
    lines.append("")
    lines.append(
        "Enforcement is **not yet active**. Run "
        "`/enforcement mode new_mode:active` when you are ready to hold "
        "low-band joiners and act on Malicious ones."
    )
    lines.append(
        "If you add new channels later, re-run this command to apply "
        "the overrides — the bot does not yet auto-provision new channels."
    )
    await interaction.followup.send("\n".join(lines), ephemeral=True)


enforcement_group = app_commands.Group(
    name="enforcement",
    description="Configure Regulus enforcement settings for this guild.",
    default_permissions=discord.Permissions(moderate_members=True),
    guild_only=True,
)


def _band_index_for_hold(band: str) -> int:
    return enforcement.BAND_ORDER.index(band)


def _describe_hold_effect(hold_below_band: str) -> str:
    hold_idx = _band_index_for_hold(hold_below_band)
    held = [b for i, b in enumerate(enforcement.BAND_ORDER) if i > hold_idx and b != "Malicious"]
    if not held:
        return "no bands would be held (only Malicious is acted on)"
    return "would hold: " + ", ".join(held)


async def _notify_enforcement_change(guild_id: int, actor_id: int, change: str) -> None:
    await _post_notice(guild_id, f"<@{actor_id}> updated enforcement: {change}")


@enforcement_group.command(
    name="show",
    description="Show the current enforcement configuration for this guild.",
)
@app_commands.checks.has_permissions(moderate_members=True)
async def enforcement_show(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    config = await db.get_guild_config(interaction.guild.id)
    role_line = "unset — run `/setup-unverified` first"
    if config.unverified_role_id:
        role_obj = interaction.guild.get_role(config.unverified_role_id)
        role_line = (
            role_obj.mention if role_obj
            else f"`{config.unverified_role_id}` (role missing?)"
        )
    lines = [
        f"**Mode:** `{config.mode}`  "
        + ("(**not enforcing** — audits only)" if config.mode == "shadow"
           else "(**enforcing** — hold / kick / ban on join per band)"),
        f"**Hold threshold:** below `{config.hold_below_band}` — "
        f"{_describe_hold_effect(config.hold_below_band)}",
        f"**Malicious action:** `{config.malicious_action}`",
        f"**Unverified role:** {role_line}",
        f"**Last updated:** `{config.updated_at}`"
        + (f" by <@{config.updated_by}>" if config.updated_by else ""),
    ]
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@enforcement_group.command(
    name="mode",
    description="Set enforcement mode.",
)
@app_commands.describe(new_mode="shadow = observe only. active = act on join.")
@app_commands.choices(new_mode=[
    app_commands.Choice(name="shadow (audit only, no enforcement)", value="shadow"),
    app_commands.Choice(name="active (assign Unverified + act on Malicious)", value="active"),
])
@app_commands.checks.has_permissions(moderate_members=True)
async def enforcement_mode_command(
    interaction: discord.Interaction,
    new_mode: app_commands.Choice[str],
) -> None:
    await interaction.response.defer(ephemeral=True)
    config = await db.get_guild_config(interaction.guild.id)
    if new_mode.value == "active" and config.unverified_role_id is None:
        await interaction.followup.send(
            "Cannot enable `active` mode: run `/setup-unverified` first so "
            "an Unverified role exists to assign.",
            ephemeral=True,
        )
        return
    await db.update_guild_config(
        interaction.guild.id,
        updated_by=interaction.user.id,
        mode=new_mode.value,
    )
    await _notify_enforcement_change(
        interaction.guild.id, interaction.user.id,
        f"mode → `{new_mode.value}`",
    )
    await interaction.followup.send(
        f"Enforcement mode set to `{new_mode.value}`.",
        ephemeral=True,
    )


@enforcement_group.command(
    name="hold_below",
    description="Hold joiners scoring worse than this band on the Unverified role.",
)
@app_commands.describe(band="Bands strictly worse than this get held on @Unverified.")
@app_commands.choices(band=[
    app_commands.Choice(name="Trusted (hold everyone below Trusted — strict)", value="Trusted"),
    app_commands.Choice(name="Likely-safe (hold Neutral / Suspicious — default)", value="Likely-safe"),
    app_commands.Choice(name="Neutral (hold only Suspicious — lenient)", value="Neutral"),
    app_commands.Choice(name="Suspicious (hold no one — very lenient)", value="Suspicious"),
])
@app_commands.checks.has_permissions(moderate_members=True)
async def enforcement_hold_below(
    interaction: discord.Interaction,
    band: app_commands.Choice[str],
) -> None:
    await interaction.response.defer(ephemeral=True)
    await db.update_guild_config(
        interaction.guild.id,
        updated_by=interaction.user.id,
        hold_below_band=band.value,
    )
    await _notify_enforcement_change(
        interaction.guild.id, interaction.user.id,
        f"hold threshold → below `{band.value}` — "
        f"{_describe_hold_effect(band.value)}",
    )
    await interaction.followup.send(
        f"Hold threshold set to `{band.value}` — "
        f"{_describe_hold_effect(band.value)}.",
        ephemeral=True,
    )


@enforcement_group.command(
    name="malicious",
    description="What to do with joiners in the Malicious band.",
)
@app_commands.describe(action="kick = remove but rejoinable. ban = permanent. hold = assign @Unverified.")
@app_commands.choices(action=[
    app_commands.Choice(name="kick (default — rejoinable)", value="kick"),
    app_commands.Choice(name="ban (permanent, delete 1 day of messages)", value="ban"),
    app_commands.Choice(name="hold (assign @Unverified — mod reviews)", value="hold"),
])
@app_commands.checks.has_permissions(moderate_members=True)
async def enforcement_malicious(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
) -> None:
    await interaction.response.defer(ephemeral=True)
    await db.update_guild_config(
        interaction.guild.id,
        updated_by=interaction.user.id,
        malicious_action=action.value,
    )
    await _notify_enforcement_change(
        interaction.guild.id, interaction.user.id,
        f"malicious action → `{action.value}`",
    )
    await interaction.followup.send(
        f"Malicious action set to `{action.value}`.",
        ephemeral=True,
    )


bot.tree.add_command(enforcement_group)


@bot.tree.command(
    name="audit-guild-blocklist",
    description="Scan this guild's current members against the blocklist and list matches.",
)
@app_commands.default_permissions(moderate_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def audit_guild_blocklist_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    matches: list[tuple[discord.Member, list[db.Flag]]] = []
    for member in interaction.guild.members:
        if member.bot:
            continue
        flags = await db.get_active_flags(member.id)
        if flags:
            matches.append((member, flags))

    if not matches:
        await interaction.followup.send(
            "No current members of this guild are on the blocklist. Clean.",
            ephemeral=True,
        )
        return

    matches.sort(key=lambda entry: len(entry[1]), reverse=True)
    lines = [f"**{len(matches)} member(s) on the blocklist:**", ""]
    for member, flags in matches[:25]:
        guild_names: list[str] = []
        for f in flags:
            g = bot.get_guild(f.guild_id)
            guild_names.append(g.name if g else f"g:{f.guild_id}")
        lines.append(
            f"• {member.mention} (`{member.id}`) — "
            f"**{len(flags)} flag(s)**: {', '.join(guild_names)}"
        )
    if len(matches) > 25:
        lines.append(f"…and {len(matches) - 25} more not shown.")

    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.command(
    name="purge-flagged",
    description="Apply this guild's malicious_action to every currently-in-guild flagged member.",
)
@app_commands.describe(
    action="dry_run lists what would happen. execute performs the actions.",
)
@app_commands.choices(action=[
    app_commands.Choice(name="dry_run (list only, no action)", value="dry_run"),
    app_commands.Choice(name="execute (apply malicious_action)", value="execute"),
])
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def purge_flagged_command(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
) -> None:
    await interaction.response.defer(ephemeral=True)
    config = await db.get_guild_config(interaction.guild.id)

    if action.value == "execute" and config.mode != "active":
        await interaction.followup.send(
            "Refusing to execute: enforcement mode is `shadow`. "
            "Run `/enforcement mode new_mode:active` first, or use `dry_run` to preview.",
            ephemeral=True,
        )
        return

    targets: list[discord.Member] = []
    for member in interaction.guild.members:
        if member.bot:
            continue
        flags = await db.get_active_flags(member.id)
        if flags:
            targets.append(member)

    if not targets:
        await interaction.followup.send(
            "No current members are on the blocklist. Nothing to purge.",
            ephemeral=True,
        )
        return

    if action.value == "dry_run":
        lines = [
            f"**Dry run — {len(targets)} flagged member(s) currently in guild.**",
            f"Configured malicious_action: `{config.malicious_action}`",
            "",
            "Would act on:",
        ]
        for m in targets[:20]:
            lines.append(f"• {m.mention} (`{m.id}`)")
        if len(targets) > 20:
            lines.append(f"…and {len(targets) - 20} more.")
        lines.append("")
        lines.append("Re-run with `action:execute` to apply.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        return

    kicked = 0
    banned = 0
    held = 0
    failed: list[str] = []

    for member in targets:
        try:
            if config.malicious_action == "ban":
                await member.ban(
                    reason=f"Regulus /purge-flagged by {interaction.user}",
                    delete_message_seconds=86400,
                )
                banned += 1
            elif config.malicious_action == "kick":
                await member.kick(
                    reason=f"Regulus /purge-flagged by {interaction.user}",
                )
                kicked += 1
            elif config.malicious_action == "hold":
                if config.unverified_role_id is None:
                    failed.append(f"{member.mention}: no Unverified role")
                    continue
                role = interaction.guild.get_role(config.unverified_role_id)
                if role is None:
                    failed.append(f"{member.mention}: Unverified role missing")
                    continue
                await member.add_roles(
                    role,
                    reason=f"Regulus /purge-flagged by {interaction.user}",
                )
                held += 1
        except discord.Forbidden:
            failed.append(f"{member.mention}: missing permission")
        except discord.HTTPException as e:
            failed.append(f"{member.mention}: {e}")

    summary_lines = [
        f"**Purge complete.** Action: `{config.malicious_action}`",
        f"Kicked: **{kicked}**  •  Banned: **{banned}**  •  Held: **{held}**",
    ]
    if failed:
        summary_lines.append(f"Failed on **{len(failed)}** member(s):")
        for f in failed[:10]:
            summary_lines.append(f"• {f}")
        if len(failed) > 10:
            summary_lines.append(f"…and {len(failed) - 10} more.")
    await interaction.followup.send("\n".join(summary_lines), ephemeral=True)

    total_acted = kicked + banned + held
    if total_acted > 0:
        await _post_notice(
            interaction.guild.id,
            f"{interaction.user.mention} ran `/purge-flagged` — "
            f"acted on {total_acted} member(s) via `{config.malicious_action}`.",
        )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "You do not have permission to use this command."
    elif isinstance(error, app_commands.NoPrivateMessage):
        message = "This command can only be used in a server."
    else:
        log.exception("app command error: %s", error)
        message = "Something went wrong running that command."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)
