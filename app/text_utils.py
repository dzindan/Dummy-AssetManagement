"""Tiny, dependency-free string helpers shared across importer/db/routes
(kept separate from importer.py to avoid a circular import with db.py)."""

import re

# "VIETNAM"/"VIETNAME" (a typo variant seen in the real data) with or without
# an internal space ("VIET NAM"), and the whole VIETNAM part is optional
# since a few rows drop it entirely (e.g. "SHINHAN BANK THU THIEM RMC").
# Deliberately does NOT match plain "SHINHAN <something else>" department
# names like "SHINHAN ACADEMY" or "SHINHAN CULTURE TEAM", which are real
# names, not a redundant bank-name prefix.
_BANK_PREFIX_RE = re.compile(r"^\s*SHINHAN\s+BANK(\s+VIET\s*NAME?)?\s+", re.IGNORECASE)


def strip_bank_prefix(name: str) -> str:
    """Branch eng_name values from the source ID file all repeat "SHINHAN
    BANK VIETNAM(E)" as a prefix, which is redundant everywhere a branch name
    is shown (Dashboard, branch pages, hand-over forms...) since the whole
    app is already scoped to this one bank."""
    return _BANK_PREFIX_RE.sub("", name or "").strip()


def normalize_user_id(raw) -> tuple[str, str]:
    """Return (raw_display, normalized_numeric_key).

    Handles the same person's ID being stored as an int (losing a leading
    zero) in one file and as a zero-padded string in another, by keying on
    the digits with leading zeros stripped. Non-numeric IDs (rare, e.g. a
    department name used as the "user") keep their normalized form as an
    uppercased/trimmed string instead. Lives here (not importer.py) so both
    importer.py and db.py can call it without a circular import - db.py
    needs it to backfill/maintain the indexed `users.user_no_norm` column
    that find_user() looks up against instead of scanning the whole table.
    """
    raw_display = "" if raw is None else str(raw).strip()
    if not raw_display:
        return "", ""
    digits = re.sub(r"\D", "", raw_display)
    if digits:
        return raw_display, str(int(digits))
    return raw_display, raw_display.upper()
