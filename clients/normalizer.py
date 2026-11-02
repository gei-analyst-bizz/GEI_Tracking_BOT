import re
import hashlib
import logging

logger = logging.getLogger(__name__)


def normalize_mobile_number(phone: str) -> str:
    """
    Normalizes any mobile number string into standard 12-digit format: 91XXXXXXXXXX
    Examples:
      '9876543210'       -> '919876543210'
      '+91 82875 11886'  -> '918287511886'
      '91 82875 11886'   -> '918287511886'
      '+91-82875-11886'  -> '918287511886'
      '08287511886'      -> '918287511886'
      '00918287511886'   -> '918287511886'
      '0918287511886'    -> '918287511886'
    """
    if not phone:
        return ""
    
    # Strip all non-digit characters
    # pyrefly: ignore [unnecessary-type-conversion]
    digits = "".join(c for c in str(phone) if c.isdigit())
    if not digits:
        return ""

    # International exit code prefix 0091 (e.g. 00918287511886 -> 14 digits)
    if digits.startswith("0091") and len(digits) == 14:
        digits = digits[2:]

    # Leading 0 followed by 91 (e.g. 0918287511886 -> 13 digits)
    if digits.startswith("091") and len(digits) == 13:
        digits = digits[1:]

    # Leading 0 (e.g. 09876543210 -> 11 digits)
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    # 10-digit standard Indian mobile number
    if len(digits) == 10:
        return "91" + digits

    # Already has 91 prefix and 12 digits
    if len(digits) == 12 and digits.startswith("91"):
        return digits

    # If international with >10 digits
    return digits


def normalize_building(building_raw: str) -> str:
    """
    Normalizes various building name spellings to standard codes:
    GEBB1, GEBB2, GETT
    """
    if not building_raw:
        return ""
    # pyrefly: ignore [unnecessary-type-conversion]
    b = str(building_raw).strip().upper()
    if (
        "GEBB II" in b
        or "GEBB-2" in b
        or "BAY 2" in b
        or "BUISNESS BAY 2" in b
        or "GEBB 2" in b
        or "BUSINESS BAY-II" in b
        or "BUSINESS BAY II" in b
        or "BUSINESS BAY-2" in b
        or "BUSINESS BAY 2" in b
        or b == "GEBB2"
    ):
        return "GEBB2"
    if (
        "GEBB I" in b
        or "GEBB-1" in b
        or "BAY 1" in b
        or "GEBB 1" in b
        or "BUSINESS BAY-I" in b
        or "BUSINESS BAY I" in b
        or "BUSINESS BAY-1" in b
        or "BUSINESS BAY 1" in b
        or b == "GEBB1"
    ):
        return "GEBB1"
    if "GETT" in b or "TERRACE" in b or "TRADE TOWER" in b:
        return "GETT"
    return b


def generate_sync_key(source_sheet: str, building: str, company: str, floor: str, unit: str, mobile: str) -> str:
    """
    Generates a deterministic unique key for a client row to support idempotent upsert.
    Ensures multiple companies or multiple units under the same mobile number each have unique records.
    """
    raw = f"{source_sheet.strip().lower()}|{building.strip().lower()}|{company.strip().lower()}|{floor.strip().lower()}|{unit.strip().lower()}|{mobile.strip()}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def normalize_client_row(row_dict: dict, source_sheet: str = "MASTER") -> tuple:
    """
    Normalizes a row dictionary from the Google Sheet MASTER tab into a standardized
    client record.

    Expected columns in MASTER:
      - 'Tenant Company Name'
      - 'Building Name'
      - 'Floor'
      - 'Unit Number'
      - 'Admin Contact Name'
      - 'Designation'
      - 'Primary Mobile Number'
      - 'Primary Contact Email ID'

    Returns:
      (record_dict, is_valid, list_of_errors)
    """
    clean_dict = {k.strip(): str(v).strip() for k, v in row_dict.items() if k}

    company_name = clean_dict.get("Tenant Company Name") or clean_dict.get("Company name") or ""
    building_raw = clean_dict.get("Building Name") or clean_dict.get("Building name") or ""
    floor = clean_dict.get("Floor") or ""
    unit_number = clean_dict.get("Unit Number") or clean_dict.get("Unit No") or ""
    admin_name = clean_dict.get("Admin Contact Name") or ""
    designation = clean_dict.get("Designation") or ""
    mobile_raw = clean_dict.get("Primary Mobile Number") or clean_dict.get("Admin Mobile Number") or clean_dict.get("Mobile Number") or ""
    email = clean_dict.get("Primary Contact Email ID") or clean_dict.get("Email ID") or clean_dict.get("Email") or ""

    normalized_mobile = normalize_mobile_number(mobile_raw)
    normalized_bldg = normalize_building(building_raw)

    errors = []
    if not company_name:
        errors.append("Missing company name")
    if not normalized_mobile:
        errors.append(f"Missing or invalid mobile number (raw: '{mobile_raw}')")
    elif len(normalized_mobile) < 10:
        errors.append(f"Mobile number too short: '{normalized_mobile}'")

    sync_key = generate_sync_key(source_sheet, normalized_bldg, company_name, floor, unit_number, normalized_mobile)

    record = {
        "building": normalized_bldg,
        "company_name": company_name,
        "floor": floor,
        "unit_number": unit_number,
        "admin_name": admin_name,
        "designation": designation,
        "mobile_number": normalized_mobile,
        "email": email,
        "factech_client_id": None,
        "is_active": True,
        "source_sheet": source_sheet,
        "sync_key": sync_key,
    }

    is_valid = len(errors) == 0
    return record, is_valid, errors
