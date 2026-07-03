import json
import logging
import os
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
) -> Optional[discord.Message]:
    """Post a plain-text notice to the guild's mod channel. If a message
    ID is given, post it as a Discord reply so mods can click through to
    the referenced audit embed."""
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
    try:
        return await channel.send(content=content, reference=reference)
    except discord.Forbidden:
        log.error("cannot post notice: missing permission in #%s", channel.name)
    except discord.HTTPException as e:
        log.error("failed to post notice: %s", e)
    return None


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
    await _post_or_update_audit(member.id, member.guild.id, embed)


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

    log_content = (
        f"{interaction.user.mention} flagged {member.mention}"
        + (f" — {reason_text}" if reason_text else "")
    )
    await _post_notice(
        interaction.guild.id,
        log_content,
        reply_to_message_id=updated.id if updated else None,
    )

    await interaction.followup.send(
        f"Added **{member}** to the blocklist.",
        ephemeral=True,
    )


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


def _make_unverified_overwrite(can_view: bool, is_voice: bool) -> discord.PermissionOverwrite:
    overwrite = discord.PermissionOverwrite()
    overwrite.view_channel = can_view
    for perm in _UNVERIFIED_DENY_KEYS:
        setattr(overwrite, perm, False)
    if is_voice:
        overwrite.speak = False
        overwrite.stream = False
        overwrite.connect = False if not can_view else None
    return overwrite


@bot.tree.command(
    name="setup-unverified",
    description="Create the @Unverified role and apply deny overrides guild-wide.",
)
@app_commands.describe(
    role_name="Name for the role (default: Unverified)",
    open_channels="Comma-separated channel names Unverified can still view (e.g. 'welcome,rules')",
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

    open_names = {
        n.strip().lower().lstrip("#")
        for n in open_channels.split(",") if n.strip()
    }

    role = discord.utils.get(guild.roles, name=role_name)
    created = False
    if role is None:
        try:
            role = await guild.create_role(
                name=role_name,
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
        can_view = channel.name.lower() in open_names
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
           else "(**enforcing** join actions)"),
        f"**Hold threshold:** below `{config.hold_below_band}` — "
        f"{_describe_hold_effect(config.hold_below_band)}",
        f"**Malicious action:** `{config.malicious_action}`",
        f"**Unverified role:** {role_line}",
        f"**Last updated:** `{config.updated_at}`"
        + (f" by <@{config.updated_by}>" if config.updated_by else ""),
        "",
        "_Note: even in `active` mode, enforcement dispatch on member join_",
        "_is not yet wired in — this will land in a subsequent commit._",
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
