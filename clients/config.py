from __future__ import annotations
import os
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


# ── Google Sheets Credentials ───────────────────────────────────────────────
TENANTS_SHEET_ID = os.getenv("TENANTS_SHEET_ID", "1nU6iZoVxJexzU9ikl84IUi57W3rjLtXff_ZBq5qJwhk")
TENANTS_SERVICE_ACCOUNT = os.getenv("TENANTS_SERVICE_ACCOUNT", "gei-clients-whatsapp-bot@gei-whatsapp-bot.iam.gserviceaccount.com")
TENANTS_PRIVATE_KEY = os.getenv("TENANTS_PRIVATE_KEY", "")
TENANTS_MASTER_TAB = "MASTER"

# ── Factech API Configuration ───────────────────────────────────────────────
FACTECH_BASE_URL = os.getenv("FACTECH_BASE_URL", "https://api.isocietymanager.com").rstrip("/")
FACTECH_API_KEY = os.getenv("FACTECH_API_KEY", "84kRVwKiBZrjJL2loRWXlhh_u6AJp_b6Wq_OKsbm250")
FACTECH_USERNAME = os.getenv("FACTECH_USERNAME", "")
FACTECH_PASSWORD = os.getenv("FACTECH_PASSWORD", "")

# Building Site IDs:
# 467 - Business Bay-I (GEBB I)
# 593 - Business Bay-II (GEBB II)
# 926 - GETT
BUILDING_SITE_MAP = {
    "GEBB1": "467",
    "GEBB I": "467",
    "GEBB-1": "467",
    "BAY 1": "467",
    "BAY-1": "467",
    "BAY1": "467",
    "BUSINESS BAY 1": "467",
    "BUSINESS BAY-1": "467",
    "BUSINESS BAY I": "467",
    "BUSINESS BAY-I": "467",
    "GEBB2": "593",
    "GEBB II": "593",
    "GEBB-2": "593",
    "BAY 2": "593",
    "BAY-2": "593",
    "BAY2": "593",
    "BUSINESS BAY 2": "593",
    "BUSINESS BAY-2": "593",
    "BUSINESS BAY II": "593",
    "BUSINESS BAY-II": "593",
    "GETT": "926",
    "GOOD EARTH TRADE TOWER": "926",
}

DEFAULT_SITE_ID = "467"

# ── Factech Complaint Nature & Sub Nature Specifications ────────────────────
COMPLAINT_NATURES = [
    "Civil",
    "Electrical",
    "Fire and Life Safety",
    "Housekeeping",
    "HVAC",
    "Miscellaneous",
    "Plumbing",
]

SUB_NATURES_ALL = [
    "AC Not Working",
    "Airflow Issue",
    "Bad Odour",
    "Carpet Cleaning (Internal)",
    "Choked Basin/Bottle Trap/Drainage",
    "Cleaning Request",
    "Emergency Exit Light",
    "Emergency Lighting",
    "Facade Glass Damage",
    "False Ceiling Access",
    "Fire Alarm Issue",
    "Fire Door / Door Closer",
    "Fire Door/Door Closer/Lobby Doors",
    "Fire Extinguisher",
    "Floor Tiles/Marble Broken/Damaged",
    "Flush Not Working",
    "Garbage Removal",
    "Lighting Not Working",
    "Lock & Key",
    "Loose Wiring / Burning Smell",
    "Low Cooling",
    "MCB Tripping",
    "MCP Glass Broken",
    "Noise from AC",
    "Other",
    "Pest Control",
    "Power Failure",
    "Smoke Detector Fault",
    "Spill Cleaning",
    "Sprinkler Leakage",
    "Switch / Socket Fault",
    "Tap Not Working",
    "Thermostat Issue",
    "Toilet / Urinal Choked",
    "UPS Power Issue",
    "Wall Crack / Require Paint",
    "Washroom Cleaning",
    "Water Leakage",
    "Water Leakage From AC",
    "Water Seepage / Dampness Spot",
    "Water Supply Issue",
]

# Targeted sub-nature lists mapped to Nature (<=10 items to comply with WhatsApp List Message constraints)
NATURE_TO_SUB_NATURES = {
    "Civil": [
        "Fire Door / Door Closer",
        "Lock & Key",
        "False Ceiling Access",
        "Floor Tiles/Marble Broken/Damaged",
        "Façade Glass Damage",
        "Wall Crack / Require Paint",
        "Water Seepage / Dampness Spot",
        "Other",
    ],
    "Electrical": [
        "Power Failure",
        "Lighting Not Working",
        "Switch/Socket Fault",
        "MCB Tripping",
        "UPS Power Issue",
        "Loose Wiring / Burning Smell",
        "Emergency Lighting",
        "Other",
    ],
    "Fire and Life Safety": [
        "Fire Alarm Issue",
        "Smoke Detector Fault",
        "MCP Glass Broken",
        "Fire Extinguisher",
        "Sprinkler Leakage",
        "Emergency Exit Light",
        "Other",
    ],
    "Fire & Life Safety": [
        "Fire Alarm Issue",
        "Smoke Detector Fault",
        "MCP Glass Broken",
        "Fire Extinguisher",
        "Sprinkler Leakage",
        "Emergency Exit Light",
        "Other",
    ],
    "General": [
        "Cleaning Request",
        "Bad Odour",
        "Power Failure",
        "AC Not Working",
        "Water Leakage",
        "Tap Not Working",
        "Other",
    ],
    "Housekeeping": [
        "Cleaning Request",
        "Spill Cleaning",
        "Washroom Cleaning",
        "Garbage Removal",
        "Pest Control",
        "Carpet Cleaning (Internal)",
        "Other",
    ],
    "HouseKeeping": [
        "Cleaning Request",
        "Spill Cleaning",
        "Washroom Cleaning",
        "Garbage Removal",
        "Pest Control",
        "Carpet Cleaning (Internal)",
        "Other",
    ],
    "HVAC": [
        "AC Not Working",
        "Low Cooling",
        "Airflow Issue",
        "Water Leakage from AC",
        "Thermostat Issue",
        "Noise from AC",
        "Other",
    ],
    "Plumbing": [
        "Water Leakage",
        "Choked Basin/Bottle Trap/Drainage",
        "Toilet / Urinal Choked",
        "Flush Not Working",
        "Water Supply Issue",
        "Tap Not Working",
        "Bad Odour",
        "Other",
    ],
    "Miscellaneous": [
        "Other",
    ],
    "Other": [
        "Other",
    ],
}

# ── Sync Configuration ──────────────────────────────────────────────────────
CLIENT_SYNC_INTERVAL_MINUTES = int(os.getenv("CLIENT_SYNC_INTERVAL_MINUTES", "10"))

def get_site_id_for_building(building: str) -> str:
    """Returns the Factech Site ID for a given building name/code.
    Business Bay-I -> 467
    Business Bay-II -> 593
    """
    if not building:
        return DEFAULT_SITE_ID
    # pyrefly: ignore [unnecessary-type-conversion]
    b = str(building).strip().upper()
    if b in BUILDING_SITE_MAP:
        return BUILDING_SITE_MAP[b]
    for k, v in BUILDING_SITE_MAP.items():
        if k.upper() == b:
            return v
    if any(m in b for m in ("BAY-II", "BAY II", "BAY 2", "BAY-2", "BAY2", "GEBB II", "GEBB 2", "GEBB-2", "GEBB2")):
        return "593"
    if any(m in b for m in ("BAY-I", "BAY I", "BAY 1", "BAY-1", "BAY1", "GEBB I", "GEBB 1", "GEBB-1", "GEBB1")):
        return "467"
    if "GETT" in b or "TERRACE" in b or "TRADE TOWER" in b:
        return "926"
    return DEFAULT_SITE_ID


# ── Chaitanya Temporary Tenant Override ─────────────────────────────────────
# When True: Chaitanya is strictly treated as a Tenant only (Factech flow & menu).
# She must NOT be treated or identified as a Team Member anywhere in GEI_BOT.
# Later, toggle this to False when instructed to treat her as a Team Member again.
TREAT_CHAITANYA_AS_TENANT_ONLY = True
CHAITANYA_PHONE = "919713957666"
CHAITANYA_ALT_PHONE = "9713957666"
CHAITANYA_USER_ID = "43fd1671-b51e-4f65-a408-fa761553c7b4"

CHAITANYA_TENANT_RECORD = {
    "building": "Business Bay-II",
    "company_name": "Good Earth Infra",
    "floor": "Ground Floor",
    "unit_number": "GEEBTWOTest",
    "admin_name": "Chaitanya Test",
    "designation": "Admin",
    "mobile_number": "919713957666",
    "email": "",
    "factech_client_id": None,
    "is_active": True,
    "source_sheet": "MASTER",
    "sync_key": "ee3f13c9e81de3b49865696727cfb658",
}


def is_chaitanya(identifier: str | None) -> bool:
    """Checks if a given phone, user_id, or name matches Chaitanya."""
    if not identifier:
        return False
    # pyrefly: ignore [unnecessary-type-conversion]
    clean = str(identifier).strip().lower()
    # Strip non-digits for phone matching
    digits = "".join(c for c in clean if c.isdigit())
    if digits in (CHAITANYA_PHONE, CHAITANYA_ALT_PHONE) or digits.endswith("9713957666"):
        return True
    if clean == CHAITANYA_USER_ID.lower():
        return True
    if clean in ("chaitanya", "chaitnaya", "chaitanya test"):
        return True
    return False


# ── Developer Tenant Testing Support ─────────────────────────────────────────
from elara.config import DEVELOPER_PHONE

DEVELOPER_TENANT_RECORD = {
    "building": "Business Bay-II",
    "company_name": "Good Earth Infra (Dev Test)",
    "floor": "Ground Floor",
    "unit_number": "GEEBTWOTest",
    "admin_name": "Abhijeet (Developer)",
    "designation": "Developer / Admin",
    "mobile_number": DEVELOPER_PHONE,
    "email": "developer@gei.com",
    "factech_client_id": None,
    "is_active": True,
    "source_sheet": "MASTER",
    "sync_key": "dev_test_factech_record",
}


def is_developer_phone(identifier: str | None) -> bool:
    """Checks if a given phone matches the developer."""
    if not identifier:
        return False
    # pyrefly: ignore [unnecessary-type-conversion]
    clean = str(identifier).strip()
    digits = "".join(c for c in clean if c.isdigit())
    return (
        digits == DEVELOPER_PHONE
        or (len(digits) >= 10 and digits.endswith(DEVELOPER_PHONE[-10:]))
        or "developer" in clean.lower()
        or "abhijeet" in clean.lower()
    )


def is_tester(identifier: str | None) -> bool:
    """Checks if a given identifier matches either of the two testers (Developer or Chaitanya)."""
    return is_chaitanya(identifier) or is_developer_phone(identifier)



