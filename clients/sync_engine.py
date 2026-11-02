from __future__ import annotations
import logging
import os
import json
import threading
import time
from datetime import datetime
from db import supabase
from clients.sheets_client import fetch_master_tenant_records
from clients.normalizer import normalize_client_row

logger = logging.getLogger(__name__)

CACHE_FILE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "clients_cache.json")

# In-memory fallback cache in case Supabase table is in transition or offline
_IN_MEMORY_CLIENTS_CACHE = []
_LAST_SYNC_TIMESTAMP: float = 0.0
_SYNC_LOCK = threading.Lock()

# Circuit breaker for Supabase 'clients' table to avoid repeated slow timeouts
_SUPABASE_CLIENTS_TABLE_AVAILABLE: bool = False
_SUPABASE_LAST_CHECK: float = 0.0
_SUPABASE_CHECK_COOLDOWN: float = 300.0  # 5 minutes
SYNC_ON_MISS_COOLDOWN_SECONDS: float = 15.0  # Debounce on-demand sync on lookup miss


def is_supabase_clients_table_available() -> bool:
    """Returns True if the Supabase clients table is verified available."""
    global _SUPABASE_CLIENTS_TABLE_AVAILABLE
    return bool(_SUPABASE_CLIENTS_TABLE_AVAILABLE)


def should_attempt_supabase_sync() -> bool:
    """Checks if we should attempt Supabase sync (periodically re-checking if table was created)."""
    global _SUPABASE_CLIENTS_TABLE_AVAILABLE, _SUPABASE_LAST_CHECK
    if _SUPABASE_CLIENTS_TABLE_AVAILABLE:
        return True
    return (time.time() - _SUPABASE_LAST_CHECK) >= _SUPABASE_CHECK_COOLDOWN


def mark_supabase_clients_table_unavailable():
    """Marks the Supabase clients table as unavailable for the cooldown period."""
    global _SUPABASE_CLIENTS_TABLE_AVAILABLE, _SUPABASE_LAST_CHECK
    _SUPABASE_CLIENTS_TABLE_AVAILABLE = False
    _SUPABASE_LAST_CHECK = time.time()


def get_last_sync_timestamp() -> float:
    """Returns the timestamp of the last successful client sync."""
    return _LAST_SYNC_TIMESTAMP


def _save_cache_to_disk(records: list):
    try:
        os.makedirs(os.path.dirname(CACHE_FILE_PATH), exist_ok=True)
        with open(CACHE_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"Failed to persist client cache to disk: {e}")


def _load_cache_from_disk() -> list:
    try:
        if os.path.exists(CACHE_FILE_PATH):
            with open(CACHE_FILE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception as e:
        logger.warning(f"Failed to load client cache from disk: {e}")
    return []



def run_client_master_sync() -> dict:
    """
    Performs full synchronization from the Google Sheet MASTER tab into Supabase.

    Steps:
      1. Fetch all rows from MASTER tab.
      2. Normalize fields, phone numbers, buildings, and compute deterministic sync_key.
      3. Validate records and log invalid rows.
      4. Upsert valid records into Supabase 'clients' table on_conflict='sync_key'.
      5. Soft-deactivate (is_active=False) any previously active clients missing from sheet.
      6. Return sync summary.
    """
    global _IN_MEMORY_CLIENTS_CACHE, _LAST_SYNC_TIMESTAMP
    start_time = datetime.now()
    _LAST_SYNC_TIMESTAMP = time.time()
    logger.info("=== Starting Client Master Sync (MASTER Tab) ===")

    stats = {
        "total_rows_read": 0,
        "valid_records": 0,
        "invalid_records": 0,
        "upserted": 0,
        "deactivated": 0,
        "errors": [],
    }

    try:
        raw_rows = fetch_master_tenant_records()
        stats["total_rows_read"] = len(raw_rows)
    except Exception as e:
        logger.error(f"Failed to fetch tenant records from Google Sheets: {e}", exc_info=True)
        stats["errors"].append(f"Sheets fetch error: {str(e)}")
        return stats

    valid_records = []
    seen_sync_keys = set()

    for row in raw_rows:
        row_num = row.get("_row_number", "?")
        normalized, is_valid, validation_errors = normalize_client_row(row, source_sheet="MASTER")

        if is_valid:
            valid_records.append(normalized)
            seen_sync_keys.add(normalized["sync_key"])
        else:
            stats["invalid_records"] += 1
            logger.warning(
                f"[SYNC] Skipped invalid row {row_num}: {', '.join(validation_errors)} | Raw: {row}"
            )
            stats["errors"].append(f"Row {row_num}: {', '.join(validation_errors)}")

    stats["valid_records"] = len(valid_records)

    # Update in-memory fallback cache and persist to disk immediately
    _IN_MEMORY_CLIENTS_CACHE = [dict(r) for r in valid_records]
    _save_cache_to_disk(_IN_MEMORY_CLIENTS_CACHE)

    # Attempt Supabase Upsert (if table is available or re-check is due)
    if should_attempt_supabase_sync():
        try:
            _SUPABASE_LAST_CHECK = time.time()
            # Check if table exists
            supabase.table("clients").select("id").limit(1).execute()
            _SUPABASE_CLIENTS_TABLE_AVAILABLE = True

            # Batch upsert valid records
            batch_size = 50
            for i in range(0, len(valid_records), batch_size):
                batch = valid_records[i : i + batch_size]
                supabase.table("clients").upsert(batch, on_conflict="sync_key").execute()
                stats["upserted"] += len(batch)

            # Deactivate clients not present in the latest sync
            active_res = supabase.table("clients").select("id, sync_key").eq("is_active", True).execute()
            active_db_clients = active_res.data or []

            stale_ids = [
                c["id"]
                for c in active_db_clients
                if c.get("sync_key") and c["sync_key"] not in seen_sync_keys
            ]

            if stale_ids:
                logger.info(f"Deactivating {len(stale_ids)} clients no longer present in sheet")
                for sid in stale_ids:
                    supabase.table("clients").update({"is_active": False, "updated_at": datetime.now().isoformat()}).eq("id", sid).execute()
                stats["deactivated"] = len(stale_ids)

            logger.info(
                f"=== Client Sync Complete: {stats['upserted']} upserted, "
                f"{stats['invalid_records']} invalid, {stats['deactivated']} deactivated in "
                f"{(datetime.now() - start_time).total_seconds():.2f}s ==="
            )

        except Exception as db_err:
            err_msg = str(db_err)
            if "PGRST205" in err_msg or "Could not find the table" in err_msg:
                mark_supabase_clients_table_unavailable()
                logger.info(
                    "Supabase 'clients' table not present in schema cache. "
                    f"In-memory sync cache will serve as authoritative store ({len(valid_records)} clients)."
                )
            else:
                logger.warning(
                    f"Supabase 'clients' table update failed (may not be created yet): {db_err}. "
                    f"In-memory fallback cache updated with {len(valid_records)} clients."
                )
            stats["errors"].append(f"Database upsert warning: {str(db_err)}")
    else:
        logger.debug(
            f"Supabase 'clients' table marked unavailable; in-memory cache has {len(valid_records)} clients."
        )

    return stats


def maybe_refresh_client_cache_on_miss() -> list:
    """
    On cache miss during client lookup, checks if Google Sheet has new rows.
    Debounced by SYNC_ON_MISS_COOLDOWN_SECONDS to prevent API throttling.
    Returns the refreshed in-memory client list.
    """
    global _LAST_SYNC_TIMESTAMP
    now = time.time()
    if (now - _LAST_SYNC_TIMESTAMP) < SYNC_ON_MISS_COOLDOWN_SECONDS:
        return get_cached_clients()

    with _SYNC_LOCK:
        now = time.time()
        if (now - _LAST_SYNC_TIMESTAMP) < SYNC_ON_MISS_COOLDOWN_SECONDS:
            return get_cached_clients()

        logger.info("[SYNC] Client lookup miss triggered on-demand sync from Google Sheet MASTER tab...")
        try:
            run_client_master_sync()
        except Exception as e:
            logger.error(f"[SYNC] On-demand sync on cache miss failed: {e}", exc_info=True)

    return get_cached_clients()


def force_client_master_sync() -> dict:
    """Forces an immediate client sync from the Google Sheet, bypassing cooldown."""
    with _SYNC_LOCK:
        return run_client_master_sync()


def get_cached_clients() -> list:
    """Returns in-memory fallback clients list, loading from disk cache if uninitialized."""
    global _IN_MEMORY_CLIENTS_CACHE
    if not _IN_MEMORY_CLIENTS_CACHE:
        disk_cached = _load_cache_from_disk()
        if disk_cached:
            _IN_MEMORY_CLIENTS_CACHE = disk_cached
    return _IN_MEMORY_CLIENTS_CACHE

