from __future__ import annotations
import re
import hashlib
import logging

logger = logging.getLogger(__name__)


def normalize_mobile_number(phone: str | int | None) -> str:
    """
    Normalizes any mobile number string into standard 12-digit format: 91XXXXXXXXXX
    Examples:
      '9876543210'        -> '919876543210'
      '+91 82875 11886'   -> '918287511886'
      '91 82875 11886'    -> '918287511886'
      '+91-82875-11886'   -> '918287511886'
      '08287511886'       -> '918287511886'
      '00918287511886'    -> '918287511886'
      '0918287511886'     -> '918287511886'
      '+91 (828) 751-1886' -> '918287511886'
    """
    if phone is None:
        return ""

    raw_str = str(phone).replace("\xa0", " ").strip()
    if not raw_str:
        return ""

    # If comma or slash separated, take the first valid number
    if "," in raw_str:
        raw_str = raw_str.split(",")[0].strip()
    elif "/" in raw_str:
        raw_str = raw_str.split("/")[0].strip()

    # Strip all non-digit characters
    digits = "".join(c for c in raw_str if c.isdigit())
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


def extract_all_mobile_numbers(phone: str | int | None) -> list[str]:
    """
    Extracts and normalizes all valid mobile numbers from a raw string or cell value.
    Supports delimiters such as commas, slashes, semicolons, ampersands, linebreaks, and 'and'.
    Returns a deduplicated list of canonical phone numbers (e.g. ['918287511886', '918708868221']).
    """
    if phone is None:
        return []

    raw_str = str(phone).replace("\xa0", " ").strip()
    if not raw_str:
        return []

    # Split by common delimiters: comma, slash, semicolon, pipe, ampersand, newline, or 'and'
    tokens = re.split(r"[,/;&|\n]|(?:\band\b)", raw_str, flags=re.IGNORECASE)
    results = []
    seen = set()

    for token in tokens:
        token = token.strip()
        if not token:
            continue
        norm = normalize_mobile_number(token)
        if norm and len(norm) >= 10 and norm not in seen:
            seen.add(norm)
            results.append(norm)

    # Fallback: if no tokens yielded numbers, try normalizing the whole string
    if not results:
        norm = normalize_mobile_number(raw_str)
        if norm and len(norm) >= 10:
            results.append(norm)

    return results


def normalize_building(building_raw: str | None) -> str:
    """
    Normalizes various building name spellings to standard codes:
    GEBB1, GEBB2, GETT
    """
    if not building_raw:
        return ""
    b = str(building_raw).replace("\xa0", " ").strip().upper()
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


def _get_val(clean_dict: dict, *candidate_keys: str) -> str:
    """Helper to extract value by searching candidate keys case-insensitively with stripped whitespace."""
    # 1. Exact match
    for k in candidate_keys:
        if k in clean_dict and clean_dict[k]:
            return str(clean_dict[k]).strip()

    # 2. Case-insensitive / normalized match
    norm_map = {
        re.sub(r"[^\w\s]", "", str(k)).strip().lower(): str(v).strip()
        for k, v in clean_dict.items()
        if k
    }
    for k in candidate_keys:
        clean_k = re.sub(r"[^\w\s]", "", k).strip().lower()
        if clean_k in norm_map and norm_map[clean_k]:
            return norm_map[clean_k]

    return ""


def normalize_client_row(row_dict: dict, source_sheet: str = "MASTER") -> tuple:
    """
    Normalizes a row dictionary from the Google Sheet into a standardized client record.
    Supports flexible column names, whitespace differences, and format variations.

    Expected columns in MASTER:
      - 'Tenant Company Name' / 'Company name'
      - 'Building Name' / 'Building'
      - 'Floor'
      - 'Unit Number' / 'Unit No'
      - 'Admin Contact Name' / 'Contact Name'
      - 'Designation'
      - 'Primary Mobile Number' / 'Admin Mobile Number' / 'Mobile Number'
      - 'Primary Contact Email ID' / 'Email ID'

    Returns:
      (record_dict, is_valid, list_of_errors)
    """
    clean_dict = {str(k).replace("\xa0", " ").strip(): str(v).replace("\xa0", " ").strip() for k, v in row_dict.items() if k}

    company_name = _get_val(
        clean_dict,
        "Tenant Company Name",
        "Company Name",
        "Company name",
        "Tenant Name",
        "Company",
    )
    building_raw = _get_val(
        clean_dict,
        "Building Name",
        "Building name",
        "Building",
    )
    floor = _get_val(clean_dict, "Floor")
    unit_number = _get_val(
        clean_dict,
        "Unit Number",
        "Unit No",
        "Unit No.",
        "Unit",
    )
    admin_name = _get_val(
        clean_dict,
        "Admin Contact Name",
        "Contact Name",
        "Admin Name",
        "Name",
    )
    designation = _get_val(
        clean_dict,
        "Designation",
        "Role",
    )
    mobile_raw = _get_val(
        clean_dict,
        "Primary Mobile Number",
        "Admin Mobile Number",
        "Mobile Number",
        "Mobile",
        "Phone Number",
        "WhatsApp Number",
        "Contact Number",
        "Phone",
    )
    sec_mobile_raw = _get_val(
        clean_dict,
        "Secondary Mobile Number",
        "Alternate Mobile Number",
        "Alternate Mobile",
        "Secondary Mobile",
        "Mobile Number 2",
        "Additional Mobile Number",
        "Additional Mobile",
        "Alternate WhatsApp Number",
    )
    email = _get_val(
        clean_dict,
        "Primary Contact Email ID",
        "Email ID",
        "Email",
        "Email Address",
    )

    # Extract all valid mobile numbers from primary and secondary fields
    primary_numbers = extract_all_mobile_numbers(mobile_raw)
    secondary_numbers = extract_all_mobile_numbers(sec_mobile_raw)

    all_numbers = []
    seen_nums = set()
    for num in primary_numbers + secondary_numbers:
        if num not in seen_nums:
            seen_nums.add(num)
            all_numbers.append(num)

    normalized_mobile = all_numbers[0] if all_numbers else normalize_mobile_number(mobile_raw)
    sec_mobile = all_numbers[1] if len(all_numbers) > 1 else ""
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
        "mobile_numbers": all_numbers,
        "secondary_mobile_number": sec_mobile,
        "raw_mobile": mobile_raw,
        "email": email,
        "factech_client_id": None,
        "is_active": True,
        "source_sheet": source_sheet,
        "sync_key": sync_key,
    }

    is_valid = len(errors) == 0
    return record, is_valid, errors
