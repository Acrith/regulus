"""Per-guild invite-uses cache.

Discord's API doesn't tell us which invite a member used when they join.
We work it out by snapshotting each invite's `uses` counter and comparing
after a member joins — whichever invite gained a use is the one used.

Limits:
  - Vanity URLs are not returned by guild.invites() and cannot be
    attributed with this technique.
  - If two joins happen between snapshots, or if the bot restarts with
    a cold cache, we may miss an attribution (returned as None).
"""

from __future__ import annotations

import logging
from typing import Optional

import discord

log = logging.getLogger("regulus.invites")

# guild_id -> {invite code -> uses count at last snapshot}
_cache: dict[int, dict[str, int]] = {}


async def refresh(guild: discord.Guild) -> None:
    try:
        invites = await guild.invites()
    except discord.Forbidden:
        log.warning(
            "cannot read invites for guild %s: bot needs Manage Guild "
            "or Manage Channels permission",
            guild.id,
        )
        return
    except discord.HTTPException as e:
        log.warning("failed to fetch invites for guild %s: %s", guild.id, e)
        return
    _cache[guild.id] = {inv.code: inv.uses for inv in invites}
    log.info("cached %d invite(s) for guild %s", len(invites), guild.id)


async def find_used(guild: discord.Guild) -> Optional[discord.Invite]:
    """Diff current invites against the cache to find the one used by the
    most recent joiner. Also refreshes the cache. Returns None if not
    determinable."""
    try:
        current = await guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        return None

    before = _cache.get(guild.id, {})
    used: Optional[discord.Invite] = None
    for inv in current:
        if inv.uses > before.get(inv.code, 0):
            used = inv
            break

    _cache[guild.id] = {inv.code: inv.uses for inv in current}
    return used


def note_created(invite: discord.Invite) -> None:
    if invite.guild is None:
        return
    _cache.setdefault(invite.guild.id, {})[invite.code] = 0


def note_deleted(invite: discord.Invite) -> None:
    if invite.guild is None:
        return
    _cache.get(invite.guild.id, {}).pop(invite.code, None)
