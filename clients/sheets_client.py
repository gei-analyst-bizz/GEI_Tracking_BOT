from __future__ import annotations
import os
import time
import logging
import threading
import gspread
from google.oauth2.service_account import Credentials
from clients.config import (
    TENANTS_SHEET_ID,
    TENANTS_SERVICE_ACCOUNT,
    TENANTS_PRIVATE_KEY,
    TENANTS_MASTER_TAB,
)

logger = logging.getLogger(__name__)

_gc = None
_ss = None
_active_sa_email = None
_lock = threading.Lock()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def invalidate_connection():
    """Resets the cached gspread client and spreadsheet to force re-authentication."""
    global _gc, _ss, _active_sa_email
    with _lock:
        _gc = None
        _ss = None
        _active_sa_email = None
        logger.info("[SHEETS] Invalided Tenants Google Sheets connection cache")


def get_active_service_account() -> str | None:
    """Returns the email of the active service account currently connected."""
    return _active_sa_email


def _get_service_account_candidates() -> list[tuple[str, str]]:
    """
    Returns candidate (client_email, private_key) pairs in priority order:
      1. TENANTS_SERVICE_ACCOUNT + TENANTS_PRIVATE_KEY (Primary)
      2. GOOGLE_SERVICE_ACCOUNT_EMAIL + GOOGLE_PRIVATE_KEY (Feedback SA fallback)
      3. FACILITIES_SERVICE_ACCOUNT_EMAIL + FACILITIES_PRIVATE_KEY (Facilities SA fallback)
    """
    candidates = []

    # 1. Primary Tenants SA
    sa1 = os.getenv("TENANTS_SERVICE_ACCOUNT", TENANTS_SERVICE_ACCOUNT)
    pk1 = os.getenv("TENANTS_PRIVATE_KEY", TENANTS_PRIVATE_KEY)
    if sa1 and pk1:
        candidates.append((sa1.strip(), pk1))

    # 2. Feedback SA fallback
    sa2 = os.getenv("GOOGLE_SERVICE_ACCOUNT_EMAIL", "")
    pk2 = os.getenv("GOOGLE_PRIVATE_KEY", "")
    if sa2 and pk2 and (sa2 != sa1 or pk2 != pk1):
        candidates.append((sa2.strip(), pk2))

    # 3. Facilities SA fallback
    sa3 = os.getenv("FACILITIES_SERVICE_ACCOUNT_EMAIL", "")
    pk3 = os.getenv("FACILITIES_PRIVATE_KEY", "") or os.getenv("FACILITIES_SERVICE_ACCOUNT_PRIVATE_KEY", "")
    if sa3 and pk3 and (sa3 != sa1 or pk3 != pk1) and (sa3 != sa2 or pk3 != pk2):
        candidates.append((sa3.strip(), pk3))

    return candidates


def _retry_on_429(func, *args, max_retries=3, **kwargs):
    """Retries a Google Sheets API call with exponential backoff on 429 rate limit or 503."""
    backoff = 1.5
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            if status_code in (429, 500, 503) and attempt < max_retries:
                wait_time = backoff * (2 ** attempt)
                logger.warning(
                    f"[SHEETS] Google API error {status_code} — retrying in {wait_time:.1f}s (attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(wait_time)
            else:
                raise
        except Exception as e:
            err_str = str(e).lower()
            if ("timed out" in err_str or "connection" in err_str) and attempt < max_retries:
                wait_time = backoff * (2 ** attempt)
                logger.warning(
                    f"[SHEETS] Network/Timeout error: {e} — retrying in {wait_time:.1f}s (attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(wait_time)
            else:
                raise


def _get_spreadsheet(force_refresh: bool = False) -> gspread.Spreadsheet:
    """
    Lazily initializes and caches the gspread client and spreadsheet with multi-credential fallback.
    If force_refresh is True, resets cached connections and reconnects.
    """
    global _gc, _ss, _active_sa_email
    if not force_refresh and _ss is not None:
        return _ss

    with _lock:
        if not force_refresh and _ss is not None:
            return _ss

        if not TENANTS_SHEET_ID:
            raise RuntimeError("TENANTS_SHEET_ID not configured in environment")

        candidates = _get_service_account_candidates()
        if not candidates:
            raise RuntimeError(
                "Tenants Google Sheets credentials not configured. "
                "Ensure TENANTS_SERVICE_ACCOUNT and TENANTS_PRIVATE_KEY (or fallback SAs) are set in environment"
            )

        last_error = None
        for sa_email, sa_pk in candidates:
            try:
                pk_clean = sa_pk.strip().strip("'").strip('"').replace("\\n", "\n")
                project_id = "gei-whatsapp-bot"
                if "@" in sa_email:
                    domain = sa_email.split("@")[1]
                    if "." in domain:
                        project_id = domain.split(".")[0]

                creds_info = {
                    "type": "service_account",
                    "project_id": project_id,
                    "private_key": pk_clean,
                    "client_email": sa_email,
                    "token_uri": "https://oauth2.googleapis.com/token",
                }

                creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
                client = gspread.authorize(creds)
                spreadsheet = client.open_by_key(TENANTS_SHEET_ID)

                _gc = client
                _ss = spreadsheet
                _active_sa_email = sa_email
                logger.info(
                    f"[SHEETS] Connected to Tenants Google Sheet: '{spreadsheet.title}' "
                    f"using service account: {sa_email}"
                )
                return _ss

            except Exception as auth_err:
                logger.warning(
                    f"[SHEETS] Failed to connect using SA {sa_email}: {auth_err}. Trying next candidate..."
                )
                last_error = auth_err

        # If all candidates failed
        _gc = None
        _ss = None
        _active_sa_email = None
        raise RuntimeError(
            f"All candidate service accounts failed to connect to Tenants Google Sheet: {last_error}"
        )


def _clean_cell(val) -> str:
    """Strips non-breaking spaces and whitespace from a cell value."""
    if val is None:
        return ""
    return str(val).replace("\xa0", " ").strip()


def fetch_master_tenant_records() -> list[dict]:
    """
    Fetches all client rows from the MASTER tab of the tenants Google Sheet.
    Returns a list of dicts with stripped keys and values.
    """
    def _do_fetch():
        ss = _get_spreadsheet()
        ws = ss.worksheet(TENANTS_MASTER_TAB)
        all_values = ws.get_all_values()

        if not all_values or len(all_values) < 2:
            logger.warning("Tenants MASTER tab has no data or only headers")
            return []

        headers = [_clean_cell(h) for h in all_values[0]]
        records = []

        for row_idx, row in enumerate(all_values[1:], start=2):
            if not any(_clean_cell(c) for c in row):
                continue  # Skip entirely blank rows

            padded_row = list(row) + [""] * (len(headers) - len(row))
            row_dict = {headers[i]: _clean_cell(padded_row[i]) for i in range(len(headers))}
            row_dict["_row_number"] = row_idx
            row_dict["_source_sheet"] = TENANTS_MASTER_TAB
            records.append(row_dict)

        logger.info(f"Fetched {len(records)} non-empty rows from Tenants MASTER sheet")
        return records

    try:
        return _retry_on_429(_do_fetch)
    except Exception as e:
        logger.error(f"[SHEETS] Error fetching from MASTER tab: {e}. Invaliding connection...")
        invalidate_connection()
        # Retry once with a freshly initialized connection
        try:
            return _retry_on_429(_do_fetch)
        except Exception as retry_err:
            logger.error(f"[SHEETS] Re-connect retry also failed: {retry_err}", exc_info=True)
            raise retry_err


def fetch_tenant_records_across_all_tabs() -> list[dict]:
    """
    Fetches client rows from MASTER tab, and also checks other tabs ('BAY 1', 'BAY 2', 'GETT')
    to ensure any client added to an individual building tab is recognized.
    """
    records = []
    seen_row_signatures = set()

    # 1. Fetch MASTER tab first (authoritative)
    master_records = fetch_master_tenant_records()
    for r in master_records:
        sig = (
            _clean_cell(r.get("Tenant Company Name") or r.get("Company name")),
            _clean_cell(r.get("Primary Mobile Number") or r.get("Admin Mobile Number")),
            _clean_cell(r.get("Unit Number") or r.get("Unit No")),
        )
        seen_row_signatures.add(sig)
        records.append(r)

    # 2. Check other building tabs for any extra entries
    building_tabs = ["BAY 1", "BAY 2", "GETT"]
    ss = _get_spreadsheet()

    for tab_name in building_tabs:
        try:
            ws = ss.worksheet(tab_name)
            all_values = ws.get_all_values()
            if not all_values or len(all_values) < 2:
                continue

            headers = [_clean_cell(h) for h in all_values[0]]
            for row_idx, row in enumerate(all_values[1:], start=2):
                if not any(_clean_cell(c) for c in row):
                    continue
                padded_row = list(row) + [""] * (len(headers) - len(row))
                row_dict = {headers[i]: _clean_cell(padded_row[i]) for i in range(len(headers))}
                row_dict["_row_number"] = row_idx
                row_dict["_source_sheet"] = tab_name

                sig = (
                    _clean_cell(row_dict.get("Tenant Company Name") or row_dict.get("Company name")),
                    _clean_cell(row_dict.get("Primary Mobile Number") or row_dict.get("Admin Mobile Number")),
                    _clean_cell(row_dict.get("Unit Number") or row_dict.get("Unit No")),
                )
                if sig not in seen_row_signatures:
                    seen_row_signatures.add(sig)
                    records.append(row_dict)
                    logger.info(f"[SHEETS] Found additional client in tab '{tab_name}': {sig}")

        except Exception as tab_err:
            logger.debug(f"[SHEETS] Tab '{tab_name}' read skipped or error: {tab_err}")

    return records
