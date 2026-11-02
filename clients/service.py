from __future__ import annotations
import logging
from db import supabase
from clients.normalizer import normalize_mobile_number
from clients import sync_engine

from clients.config import (
    TREAT_CHAITANYA_AS_TENANT_ONLY,
    CHAITANYA_TENANT_RECORD,
    is_chaitanya,
)

logger = logging.getLogger(__name__)

# Temporary in-memory state for selected client context when one number has multiple units
_SELECTED_CLIENT_CONTEXT = {}  # type: dict[str, dict]


def _filter_matching_clients(records: list, target_digits: str, target_last10: str) -> list:
    """Helper to match active clients against target digits and last 10 digits."""
    matches = []
    for c in records:
        if not c.get("is_active", True):
            continue
        c_mobile = "".join(ch for ch in str(c.get("mobile_number", "")) if ch.isdigit())
        if c_mobile == target_digits or (target_last10 and len(c_mobile) >= 10 and c_mobile[-10:] == target_last10):
            rec = dict(c)
            # Map building code to standard display name
            if rec.get("building") in ("GEBB2", "GEBB II", "GEBB-2"):
                rec["building"] = "Business Bay-II"
            elif rec.get("building") in ("GEBB1", "GEBB I", "GEBB-1"):
                rec["building"] = "Business Bay-I"
            matches.append(rec)
    return matches


def lookup_clients_by_phone(phone: str) -> list:
    """
    Looks up active client records by phone number.
    Normalizes the phone number first to ensure consistent matching.
    Tries Supabase first if table is available; falls back to in-memory sync cache
    with automatic on-demand refresh on cache miss.
    """
    clean_num = normalize_mobile_number(phone)
    if not clean_num:
        return []

    # 0b. Developer Test Tenant Record (only active when in factech mode)
    from clients.config import is_developer_phone, DEVELOPER_TENANT_RECORD
    if is_developer_phone(phone) or is_developer_phone(clean_num):
        from elara.team_router import get_active_team
        if get_active_team(phone) == "factech" or get_active_team(clean_num) == "factech":
            return [dict(DEVELOPER_TENANT_RECORD)]
        return []

    # 1. Query Supabase (only if clients table is available in schema cache)
    if sync_engine.is_supabase_clients_table_available():
        try:
            res = (
                supabase.table("clients")
                .select("*")
                .eq("mobile_number", clean_num)
                .eq("is_active", True)
                .execute()
            )
            if res.data:
                return res.data
        except Exception as e:
            sync_engine.mark_supabase_clients_table_unavailable()
            logger.debug(f"Supabase client lookup error (using fallback): {e}")

    # 2. Check in-memory cache synced from authoritative client sheet
    cached = sync_engine.get_cached_clients()
    if not cached:
        try:
            sync_engine.run_client_master_sync()
            cached = sync_engine.get_cached_clients()
        except Exception as sync_err:
            logger.debug(f"Auto-sync on empty client cache failed: {sync_err}")

    # pyrefly: ignore[unnecessary-type-conversion]
    target_digits = "".join(ch for ch in str(clean_num) if ch.isdigit())
    target_last10 = target_digits[-10:] if len(target_digits) >= 10 else ""

    matches = _filter_matching_clients(cached, target_digits, target_last10)

    # 3. On-demand cache refresh on miss (recognizes newly added clients in Google Sheet immediately)
    if not matches:
        refreshed = sync_engine.maybe_refresh_client_cache_on_miss()
        if refreshed:
            matches = _filter_matching_clients(refreshed, target_digits, target_last10)
            if matches:
                logger.info(
                    f"Client {phone} recognized via on-demand Google Sheet refresh: "
                    f"{matches[0].get('admin_name')} ({matches[0].get('company_name')})"
                )

    if matches:
        return matches

    # 4. Fallback safety net for Chaitanya if cache uninitialized
    if TREAT_CHAITANYA_AS_TENANT_ONLY and (is_chaitanya(phone) or is_chaitanya(clean_num)):
        return [dict(CHAITANYA_TENANT_RECORD)]

    logger.info(f"Unregistered client lookup for phone={phone} (canonical={clean_num}) — no match in {len(cached)} records")
    return []



def get_client_by_id(client_id: str, phone: str = "") -> dict | None:
    """Retrieves a specific client record by its ID or sync_key."""
    if not client_id:
        return None

    try:
        res = supabase.table("clients").select("*").eq("id", client_id).execute()
        if res.data:
            return res.data[0]
    except Exception:
        pass

    # Fallback search by sync_key or ID in cache
    for c in get_cached_clients():
        if c.get("id") == client_id or c.get("sync_key") == client_id:
            return c

    # Fallback search by phone
    if phone:
        clients = lookup_clients_by_phone(phone)
        for c in clients:
            # pyrefly: ignore [unnecessary-type-conversion]
            if str(c.get("id")) == str(client_id) or c.get("sync_key") == client_id:
                return c

    return None


def get_active_client_context(phone: str) -> dict | None:
    """
    Gets the current active client context for the sender phone.
    If the user previously selected a unit in a multi-unit scenario, returns that.
    If only one unit exists, returns that.
    If multiple exist and none selected, returns None (triggering selection prompt).
    """
    clean_num = normalize_mobile_number(phone)
    if not clean_num:
        return None

    from clients.config import is_developer_phone, DEVELOPER_TENANT_RECORD
    if is_developer_phone(phone) or is_developer_phone(clean_num):
        from elara.team_router import get_active_team
        if get_active_team(phone) == "factech" or get_active_team(clean_num) == "factech":
            return dict(DEVELOPER_TENANT_RECORD)
        return None

    # 1. Check in-memory selected context
    if clean_num in _SELECTED_CLIENT_CONTEXT:
        return _SELECTED_CLIENT_CONTEXT[clean_num]

    # 2. Check wa_task_states metadata
    try:
        state_res = (
            supabase.table("wa_task_states")
            .select("metadata")
            .eq("whatsapp_number", clean_num)
            .execute()
        )
        if state_res.data:
            meta = state_res.data[0].get("metadata") or {}
            client_ctx = meta.get("client_context")
            if client_ctx and isinstance(client_ctx, dict):
                _SELECTED_CLIENT_CONTEXT[clean_num] = client_ctx
                return client_ctx
    except Exception:
        pass

    # 3. Lookup all records for this phone from authoritative client records
    clients = lookup_clients_by_phone(clean_num)
    if len(clients) == 1:
        _SELECTED_CLIENT_CONTEXT[clean_num] = clients[0]
        return clients[0]

    # 4. Fallback safety net for Chaitanya if cache uninitialized
    if TREAT_CHAITANYA_AS_TENANT_ONLY and (is_chaitanya(phone) or is_chaitanya(clean_num)):
        return dict(CHAITANYA_TENANT_RECORD)

    return None


def set_active_client_context(phone: str, client: dict):
    """Stores the active client context for the sender phone."""
    clean_num = normalize_mobile_number(phone)
    _SELECTED_CLIENT_CONTEXT[clean_num] = client

    if TREAT_CHAITANYA_AS_TENANT_ONLY and (is_chaitanya(phone) or is_chaitanya(clean_num)):
        return

    try:
        payload = {
            "whatsapp_number": clean_num,
            "action": "CLIENT_SESSION",
            "metadata": {"client_context": client},
        }
        supabase.table("wa_task_states").upsert(
            payload, on_conflict="whatsapp_number"
        ).execute()
    except Exception as e:
        logger.warning(f"Could not persist client context to wa_task_states: {e}")


def clear_client_context(phone: str):
    """Clears the active client context for the sender."""
    clean_num = normalize_mobile_number(phone)
    if clean_num in _SELECTED_CLIENT_CONTEXT:
        del _SELECTED_CLIENT_CONTEXT[clean_num]
