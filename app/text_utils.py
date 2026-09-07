"""Tiny, dependency-free string helpers shared across importer/db/routes
(kept separate from importer.py to avoid a circular import with db.py)."""

import datetime as dt
import ipaddress
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


_IP_MULTI_DOT_RE = re.compile(r"\.{2,}")


def clean_ip(value) -> str:
    """Clean a free-text "IP" cell from an imported asset report.

    Collapses an accidental double-dot typo (e.g. "10.95..71.209" ->
    "10.95.71.209" - a real pattern seen in the source Excel files) and then
    validates the result is an actual IP address. Anything still not valid
    after that - a placeholder someone typed instead of a real IP ("DYNAMIC
    IP", "Dây"), or an incomplete address missing an octet ("10.95.71.") -
    is dropped to blank rather than kept as literal text.

    This is deliberately different from device_name/status/model_device,
    which always keep the raw value even when unrecognized (see
    importer.py's normalize_* functions): those are free-text descriptions
    with display/audit value on their own. An IP cell has none - it exists
    only to be handed straight to Network Check as a scan target (see
    routes/network_check.py's start_scan, which pings whatever non-empty
    string is stored here), so keeping garbage would just make a real,
    reachable asset misreport as "No response", indistinguishable from one
    that's genuinely offline. Lives here (not importer.py) so both
    importer.py and db.py can call it without a circular import - db.py
    uses it to self-heal already-imported bad values, the same reasoning as
    normalize_user_id above.
    """
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    candidate = _IP_MULTI_DOT_RE.sub(".", text).strip(" .")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return ""
    return candidate


_HANDOVER_NA_TOKENS = {"NA", "N/A", "NONE", "NULL", "-", "--", "N.A", "N.A."}
_HANDOVER_FULL_DATE_FORMATS = ("%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d", "%d-%m-%Y")
_HANDOVER_YEAR_ONLY_RE = re.compile(r"^\d{4}$")
_HANDOVER_MONTH_YEAR_RE = re.compile(r"^(\d{1,2})[/-](\d{4})$")  # e.g. "03/2020", "3-2020"
_HANDOVER_YEAR_MONTH_RE = re.compile(r"^(\d{4})[/-](\d{1,2})$")  # e.g. "2020-03", "2020/3"


def normalize_handover_date(value) -> str:
    """Canonical dd/mm/yyyy form for a raw Handover Date cell/field - the
    source Excel column (and the odd manual edit) sometimes carries a full
    date, just a year, just a month+year, or nothing at all, and this app
    always displays/exports whatever's stored here as-is (Manage Assets,
    Branch Detail, their exports, the diff report) rather than reformatting
    at each call site, so the stored value itself needs to already be in one
    consistent shape:
    - a full date, in any of the formats real import files use -> dd/mm/yyyy
    - a bare year ("2020") -> 01/01/2020 (year-only is the common case for
      old/legacy handovers where nobody recorded the exact day)
    - a month+year ("03/2020" or "2020-03") -> 01/03/2020
    - blank, or a "no value" placeholder someone typed instead (NA, N/A,
      "-", ...) -> "NA"
    Anything else that doesn't match one of these shapes is kept as its
    original raw text rather than dropped or mangled - same "never silently
    lose a real value" rule as importer.py's device/status/model
    normalization."""
    if isinstance(value, (dt.datetime, dt.date)):
        return value.strftime("%d/%m/%Y")
    text = "" if value is None else str(value).strip()
    if not text or text.upper() in _HANDOVER_NA_TOKENS:
        return "NA"
    for fmt in _HANDOVER_FULL_DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    if _HANDOVER_YEAR_ONLY_RE.match(text):
        return f"01/01/{text}"
    m = _HANDOVER_MONTH_YEAR_RE.match(text)
    if m and 1 <= int(m.group(1)) <= 12:
        return f"01/{int(m.group(1)):02d}/{m.group(2)}"
    m = _HANDOVER_YEAR_MONTH_RE.match(text)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"01/{int(m.group(2)):02d}/{m.group(1)}"
    return text


def usage_duration_years(handover_date: str) -> str:
    """How long a device has been in use, as a plain calendar-year count
    (current year minus the handover_date's own year), not a precise
    day-accurate elapsed time. `handover_date` is expected already in this
    module's normalize_handover_date() form (dd/mm/yyyy, or "NA"), so the
    year is its last 4 characters, not its first 4 - blank/"NA"/
    unparseable/future-dated input returns "" so callers (Manage Assets
    table, Branch Detail, their exports) render a plain dash rather than a
    bogus "0 years"."""
    text = (handover_date or "").strip()
    if len(text) < 4 or text.upper() == "NA" or not text[-4:].isdigit():
        return ""
    years = dt.date.today().year - int(text[-4:])
    if years < 0:
        return ""
    return f"{years} year" if years == 1 else f"{years} years"
