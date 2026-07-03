"""Enforcement configuration constants and (later) dispatch.

For now this module only exposes the whitelisted values that /enforcement
subcommands accept. The actual "assign role / kick / ban" dispatch on
member join will land in a subsequent commit.
"""

from __future__ import annotations

MODES = ("shadow", "active")

# Ordered strictest to most lenient. hold_below_band means: bands worse
# than this (strictly to the right in this tuple) get held on @Unverified.
BAND_ORDER = ("Trusted", "Likely-safe", "Neutral", "Suspicious", "Malicious")
HOLD_THRESHOLDS = ("Trusted", "Likely-safe", "Neutral", "Suspicious")

MALICIOUS_ACTIONS = ("kick", "ban", "hold")
