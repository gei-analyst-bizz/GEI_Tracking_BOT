"""
Facilities Module — Configuration
===================================
All env vars, column mappings, building constants, and valid status/type
values for the Facilities department's Google Sheets integration.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Optional

# pyrefly: ignore [missing-import]
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ── Google Sheets ────────────────────────────────────────────────────────────
# Spreadsheet ID for Facilities_Master_Tracker_Final
FACILITIES_SHEET_ID = os.getenv("FACILITIES_SHEET_ID", "")
FACILITIES_API_KEY = os.getenv("FACILITIES_API_KEY", "")

# Service Account credentials for Facilities module
_fac_sa_email = (
    os.getenv("FACILITIES_SERVICE_ACCOUNT_EMAIL") or
    os.getenv("FACILITIES_SA_EMAIL")
)
_fac_sa_key = (
    os.getenv("FACILITIES_SERVICE_ACCOUNT_PRIVATE_KEY") or
    os.getenv("FACILITIES_SA_PRIVATE_KEY") or
    os.getenv("FACILITIES_PRIVATE_KEY")
)

if _fac_sa_email:
    FACILITIES_SA_EMAIL = _fac_sa_email
    # Use dedicated key if provided, else fall back to GOOGLE_PRIVATE_KEY
    FACILITIES_SA_PRIVATE_KEY = _fac_sa_key or os.getenv("GOOGLE_PRIVATE_KEY", "")
else:
    FACILITIES_SA_EMAIL = os.getenv("GOOGLE_SERVICE_ACCOUNT_EMAIL", "")
    FACILITIES_SA_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY", "")

# ── Polling ──────────────────────────────────────────────────────────────────
# How often (seconds) the sync engine polls the Sheet for external edits
FACILITIES_POLL_INTERVAL = int(os.getenv("FACILITIES_POLL_INTERVAL", "75"))

# Max retries for failed sync_queue items before giving up
FACILITIES_MAX_RETRIES = int(os.getenv("FACILITIES_MAX_RETRIES", "5"))

# Conflict detection window: if two sources edit the same field within this
# many seconds, it's flagged as a conflict rather than an overwrite
CONFLICT_WINDOW_SECONDS = int(os.getenv("FACILITIES_CONFLICT_WINDOW", "60"))

# ── Facilities EOD Report Delivery ───────────────────────────────────────────
# Evening Facilities EOD report is disabled for now.
# To re-enable, set ENABLE_FACILITIES_EOD_REPORT=true in .env
ENABLE_FACILITIES_EOD_REPORT = os.getenv("ENABLE_FACILITIES_EOD_REPORT", "false").lower() in ("true", "1", "yes")

# ── Building Tabs ────────────────────────────────────────────────────────────
# Exact tab names on the Google Sheet (must match the sheet exactly)
BUILDING_TABS = ["GEBB1", "GEBB2", "GETT", "Common"]

# For fuzzy matching when users type building names in free text.
# Also used by alias_normalizer for voice-transcript normalization.
BUILDING_ALIASES = {
    # ── GEBB1 ────────────────────────────────────────────────────────────
    "gebb1": "GEBB1",
    "gebb 1": "GEBB1",
    "bay 1": "GEBB1",
    "bay1": "GEBB1",
    "bay one": "GEBB1",
    "business bay 1": "GEBB1",
    "business bay one": "GEBB1",
    "good earth business bay 1": "GEBB1",
    "good earth business bay one": "GEBB1",
    # ── GEBB2 ────────────────────────────────────────────────────────────
    "gebb2": "GEBB2",
    "gebb 2": "GEBB2",
    "bay 2": "GEBB2",
    "bay2": "GEBB2",
    "bay two": "GEBB2",
    "business bay 2": "GEBB2",
    "business bay two": "GEBB2",
    "good earth business bay 2": "GEBB2",
    "good earth business bay two": "GEBB2",
    # ── GETT ─────────────────────────────────────────────────────────────
    "gett": "GETT",
    "tech tower": "GETT",
    "trade tower": "GETT",
    "trade tower building": "GETT",
    "good earth tech tower": "GETT",
    "good earth trade tower": "GETT",
    "getting": "GETT",              # common STT misheard variant
    "get tower": "GETT",            # common STT misheard variant
    # ── Common ───────────────────────────────────────────────────────────
    "common": "Common",
    "common area": "Common",
    "common areas": "Common",
}

# Building display mapping
BUILDING_NAME_MAPPING = {
    "GEBB1": "Bay 1",
    "GEBB2": "Bay 2",
    "GETT": "Trade Tower",
    "Common": "Common Area",
}

# ── Anoop Configuration (Sole Recipient for Facilities Sheet Change Alerts) ──
DEFAULT_ANOOP_PHONE = "919211501013"
ANOOP_WHATSAPP_NUMBER = os.getenv("ANOOP_WHATSAPP_NUMBER", DEFAULT_ANOOP_PHONE)


def resolve_anoop_phone_number() -> str:
    """
    Resolve Anoop Sir's phone number.
    Priority:
      1. Configured ANOOP_WHATSAPP_NUMBER if set
      2. Supabase users table lookup for Anoop
      3. Fallback constant (DEFAULT_ANOOP_PHONE)
    """
    from whatsapp.ux import clean_phone_number
    if ANOOP_WHATSAPP_NUMBER and ANOOP_WHATSAPP_NUMBER.strip():
        return clean_phone_number(ANOOP_WHATSAPP_NUMBER)

    try:
        from db import supabase
        res = (
            supabase.table("users")
            .select("whatsapp_number, name")
            .ilike("name", "%anoop%")
            .execute()
        )
        if res.data and res.data[0].get("whatsapp_number"):
            num = clean_phone_number(res.data[0]["whatsapp_number"])
            return num
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to resolve Anoop's phone number from DB: {e}")

    return clean_phone_number(DEFAULT_ANOOP_PHONE)


# ── Column Mapping ───────────────────────────────────────────────────────────
# All building tabs (GEBB1, GEBB2, GETT, Common) share the exact same 12 columns (A to L):
#   A: Ref. No.   B: Type   C: Key Issue / Action   D: Latest Update
#   E: Added By   F: Owner  G: Date Raised          H: Planned Date
#   I: Delay Days J: Status K: Estimated Completion Date  L: Actual Completion Date

COLUMN_MAP = {
    "A": "ref_no",
    "B": "type",
    "C": "issue_action",
    "D": "latest_update",
    "E": "last_modified_by_at",   # "Added By" on the sheet
    "F": "owner",
    "G": "created_date",          # "Date Raised" on the sheet
    "H": "planned_date",          # "Planned Date" on the sheet (formerly Target Date)
    "I": "delay_days",            # computed field — not stored in row_cache
    "J": "status",
    "K": "estimated_completion_date",
    "L": "actual_completion_date",
}

# pyrefly: ignore [bad-function-definition]
def get_column_map(building: str = None) -> dict:
    """Get the column map for a building tab (uniform across all tabs)."""
    return COLUMN_MAP

# Reverse mapping: field name → column letter
FIELD_TO_COLUMN = {v: k for k, v in COLUMN_MAP.items()}
FIELD_TO_COLUMN["target_date"] = "H"  # Backward compatibility alias

# Column indices (1-based, for gspread)
COLUMN_INDEX = {
    "ref_no": 1,
    "type": 2,
    "issue_action": 3,
    "latest_update": 4,
    "last_modified_by_at": 5,   # "Added By"
    "owner": 6,
    "created_date": 7,          # "Date Raised"
    "planned_date": 8,
    "target_date": 8,           # Backward compatibility alias
    "delay_days": 9,
    "status": 10,
    "estimated_completion_date": 11,
    "actual_completion_date": 12,
}

# pyrefly: ignore [bad-function-definition]
def get_column_index(building: str = None) -> dict:
    """Get the column index mapping for a building tab (uniform across all tabs)."""
    return COLUMN_INDEX

# Fields that are writable from GEI_BOT (read-only fields excluded; actual_completion_date is never writable)
WRITABLE_FIELDS = [
    "type", "issue_action", "latest_update", "owner",
    "planned_date", "status",
    "estimated_completion_date",
]


def parse_facilities_date(text: str) -> Optional[str]:
    """
    Parse human/NLP language dates into standard Sheet format: DD-Mon-YYYY (e.g. 10-Sep-2026).
    Supports:
        - "10th September", "10 September", "10 Sep"
        - "by Friday", "next Monday", "this Friday"
        - "15 Sep 2026", "10-Sep-2026", "20/09/2026", "2026-09-15"
        - "completion expected on 20th September"
        - "tomorrow", "day after tomorrow", "next week"
        - returns None for skip/empty/unparseable
    """
    if not text:
        return None
    clean = text.strip()
    if clean.lower() in ("skip", "no", "none", "na", "-", "—", "cancel", "keep"):
        return None

    from core.utils import parse_human_date
    try:
        from dateutil import parser as du_parser
    except ImportError:
        du_parser = None

    # Clean ordinals like 1st, 2nd, 3rd, 10th
    t_clean = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', clean, flags=re.IGNORECASE)

    # 1. Try core parse_human_date
    p = parse_human_date(t_clean)
    if p:
        try:
            dt = datetime.strptime(p, "%Y-%m-%d")
            return dt.strftime("%d-%b-%Y")
        except Exception:
            pass

    # 2. Check for DD-Mon-YYYY / DD-Month-YYYY
    m = re.search(r'\b(\d{1,2})[-/ ]([A-Za-z]{3,})[-/ ](\d{2,4})\b', clean)
    if m:
        try:
            d_str = f"{m.group(1)} {m.group(2)} {m.group(3)}"
            if du_parser:
                dt = du_parser.parse(d_str, dayfirst=True)
                return dt.strftime("%d-%b-%Y")
            else:
                for fmt in ("%d %b %Y", "%d %B %Y", "%d %b %y", "%d %B %y"):
                    try:
                        return datetime.strptime(d_str, fmt).strftime("%d-%b-%Y")
                    except ValueError:
                        pass
        except Exception:
            pass

    # 3. Handle 'next week'
    if "next week" in clean.lower():
        return (date.today() + timedelta(days=7)).strftime("%d-%b-%Y")

    # 4. Fallback to fuzzy dateutil parser
    if du_parser:
        try:
            dt = du_parser.parse(clean, fuzzy=True, dayfirst=True)
            return dt.strftime("%d-%b-%Y")
        except Exception:
            pass

    return None

# ── Valid Statuses ───────────────────────────────────────────────────────────
# These must match the Google Sheet 'Lists' tab valid status list
VALID_STATUSES = [
    "Open",
    "WIP",
    "On Hold",
    "Closed",
]

# Statuses that are considered "closed/completed" — excluded from overdue results
CLOSED_STATUSES = ["Closed", "Completed", "Done"]

# Status transitions that are always valid
STATUS_TRANSITIONS = {
    "Open": ["WIP", "Closed", "On Hold"],
    "WIP": ["Closed", "On Hold", "Open"],
    "On Hold": ["Open", "WIP", "Closed"],
    "Closed": ["Open"],  # Reopening requires explicit confirm
}

# ── Valid Task Types ─────────────────────────────────────────────────────────
# Exact types from the Google Sheet 'Lists' tab (matches Summary formulas & dropdowns)
VALID_TASK_TYPES = [
    "Project",
    "Client Escalation",
    "Management Discussion",
    "Improvement / Initiative",
    "Major Concern",
    "Other",
]

# ── Valid Owner Positions ───────────────────────────────────────────────────
# Exact owner positions allowed in the Google Sheet 'Owner' column dropdown
VALID_OWNER_POSITIONS = [
    "Facility Manager",
    "Facility Head",
    "Facilities Director",
]

# ── RAG Status Mapping ───────────────────────────────────────────────────────
# Used for the status chips in task lists (Screen 02)
RAG_STATUS_MAP = {
    "Open": {"emoji": "🔴", "label": "Open"},
    "WIP": {"emoji": "🟡", "label": "WIP"},
    "On Hold": {"emoji": "⚪", "label": "On Hold"},
    "Closed": {"emoji": "🟢", "label": "Closed"},
}

# ── Ref No Format ────────────────────────────────────────────────────────────
REF_NO_SEPARATOR = "-"
REF_NO_PAD_WIDTH = 3  # GEBB1-001, GETT-042

# ── LLM ──────────────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQCLOUD_API_KEY = os.getenv("GROQCLOUD_API_KEY", os.getenv("GROQ_API_KEY", ""))
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini")
FACILITIES_GROQ_MODEL = os.getenv("FACILITIES_GROQ_MODEL", "openai/gpt-oss-120b")
FACILITIES_GROQ_FALLBACK_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "llama-3.3-70b-versatile",
]
FACILITIES_GEMINI_MODEL = os.getenv("FACILITIES_GEMINI_MODEL", "gemini-2.5-flash")

# ── Supabase Storage ─────────────────────────────────────────────────────────
ATTACHMENTS_BUCKET = "facilities-attachments"
MAX_ATTACHMENT_SIZE_MB = 10
