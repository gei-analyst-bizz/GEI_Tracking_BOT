from __future__ import annotations
import pytest
from unittest.mock import patch, MagicMock

from clients.normalizer import (
    normalize_mobile_number,
    normalize_building,
    normalize_client_row,
)
from clients.service import lookup_clients_by_phone, get_active_client_context
from clients.flows import is_registered_client, handle_client_hi, send_unregistered_client_message
from clients.sync_engine import run_client_master_sync, get_cached_clients


# ── Safwan & Phone Normalization Tests ─────────────────────────────────────────

def test_safwan_phone_number_normalization_variants():
    """
    Verifies that all standard representations of Safwan's number (+91 82875 11886)
    consistently resolve to canonical '918287511886'.
    """
    variants = [
        "+91 82875 11886",
        "918287511886",
        "91-82875-11886",
        "82875 11886",
        "8287511886",
        "+918287511886",
        "08287511886",
        "00918287511886",
        "0918287511886",
        "+91 (828) 751-1886",
    ]
    for raw in variants:
        canonical = normalize_mobile_number(raw)
        assert canonical == "918287511886", f"Failed to normalize variant: {raw}"


def test_safwan_google_sheet_row_normalization():
    """
    Verifies that Safwan's raw row from the Google Sheet MASTER tab is correctly
    parsed, validated, and normalized with building, unit, company, and phone.
    """
    raw_sheet_row = {
        "Tenant Company Name": "Good Earth Infra",
        "Building Name": "GEBB II",
        "Floor": "Ground Floor",
        "Unit Number": "GEEBTWOTest",
        "Admin Contact Name": "safwan",
        " Designation": "Admin",
        "Primary Mobile Number": "8287511886",
        "Primary Contact Email ID": "",
        "_row_number": 87,
    }

    record, is_valid, errors = normalize_client_row(raw_sheet_row, source_sheet="MASTER")
    assert is_valid is True
    assert errors == []
    assert record["admin_name"] == "safwan"
    assert record["company_name"] == "Good Earth Infra"
    assert record["building"] == "GEBB2"
    assert record["unit_number"] == "GEEBTWOTest"
    assert record["mobile_number"] == "918287511886"
    assert record["is_active"] is True
    assert record["sync_key"] is not None


# ── Client Lookup & Recognition Tests ─────────────────────────────────────────

def test_safwan_recognized_from_all_phone_formats(monkeypatch, mock_supabase):
    """
    Tests that Safwan is recognized across any formatting (+91, without +91, spaces, hyphens).
    """
    # Seed cache with Safwan and another tenant
    safwan_rec = {
        "building": "GEBB2",
        "company_name": "Good Earth Infra",
        "floor": "Ground Floor",
        "unit_number": "GEEBTWOTest",
        "admin_name": "safwan",
        "designation": "Admin",
        "mobile_number": "918287511886",
        "email": "",
        "factech_client_id": None,
        "is_active": True,
        "source_sheet": "MASTER",
        "sync_key": "safwan_sync_key",
    }
    other_rec = {
        "building": "GEBB1",
        "company_name": "HDFC Bank",
        "floor": "Ground Floor",
        "unit_number": "R1/R2",
        "admin_name": "Rakesh Kumar",
        "designation": "Admin",
        "mobile_number": "918826896085",
        "email": "rakesh@hdfcbank.in",
        "factech_client_id": None,
        "is_active": True,
        "source_sheet": "MASTER",
        "sync_key": "hdfc_sync_key",
    }

    monkeypatch.setattr("clients.sync_engine.get_cached_clients", lambda: [safwan_rec, other_rec])

    test_inputs = [
        "+91 82875 11886",
        "8287511886",
        "918287511886",
        "82875 11886",
        "+91-82875-11886",
        "08287511886",
    ]

    for inp in test_inputs:
        matches = lookup_clients_by_phone(inp)
        assert len(matches) == 1, f"Failed lookup for input: {inp}"
        match = matches[0]
        assert match["admin_name"] == "safwan"
        assert match["company_name"] == "Good Earth Infra"
        assert match["building"] == "Business Bay-II"  # standard display name
        assert match["unit_number"] == "GEEBTWOTest"
        assert is_registered_client(inp) is True


def test_existing_registered_client_continues_to_work(monkeypatch, mock_supabase):
    """Verifies existing client continues to work normally."""
    hdfc_rec = {
        "building": "GEBB1",
        "company_name": "HDFC Bank",
        "floor": "Ground Floor",
        "unit_number": "R1/R2",
        "admin_name": "Rakesh Kumar",
        "designation": "Admin",
        "mobile_number": "918826896085",
        "email": "rakesh@hdfcbank.in",
        "factech_client_id": None,
        "is_active": True,
        "source_sheet": "MASTER",
        "sync_key": "hdfc_sync_key",
    }
    monkeypatch.setattr("clients.sync_engine.get_cached_clients", lambda: [hdfc_rec])

    matches = lookup_clients_by_phone("+91 88268 96085")
    assert len(matches) == 1
    assert matches[0]["company_name"] == "HDFC Bank"
    assert matches[0]["building"] == "Business Bay-I"
    assert is_registered_client("918826896085") is True


def test_unregistered_number_rejected_and_receives_unregistered_message(monkeypatch, mock_supabase):
    """Verifies that genuinely unregistered numbers return no matches and trigger unregistered response."""
    monkeypatch.setattr("clients.sync_engine.get_cached_clients", lambda: [])
    monkeypatch.setattr("clients.sync_engine.maybe_refresh_client_cache_on_miss", lambda: [])

    unreg_phone = "+91 99999 00000"
    matches = lookup_clients_by_phone(unreg_phone)
    assert matches == []
    assert is_registered_client(unreg_phone) is False

    with patch("clients.flows.send_text") as mock_send:
        handle_client_hi(unreg_phone)
        assert mock_send.called
        sent_body = mock_send.call_args[0][1]
        assert "couldn't find your details" in sent_body.lower()
        assert "concerned admin/support team" in sent_body.lower()


def test_inactive_client_is_not_recognized(monkeypatch, mock_supabase):
    """Verifies that an inactive client (is_active=False) is excluded from lookup."""
    inactive_rec = {
        "building": "GEBB2",
        "company_name": "Former Tenant",
        "floor": "1st Floor",
        "unit_number": "101",
        "admin_name": "Old Admin",
        "designation": "Admin",
        "mobile_number": "918287511886",
        "email": "",
        "factech_client_id": None,
        "is_active": False,  # Deactivated
        "source_sheet": "MASTER",
        "sync_key": "old_sync_key",
    }
    monkeypatch.setattr("clients.sync_engine.get_cached_clients", lambda: [inactive_rec])
    monkeypatch.setattr("clients.sync_engine.maybe_refresh_client_cache_on_miss", lambda: [inactive_rec])

    matches = lookup_clients_by_phone("+91 82875 11886")
    assert matches == []
    assert is_registered_client("+91 82875 11886") is False


def test_missing_mandatory_fields_invalid_row():
    """Verifies that rows missing mandatory fields (mobile or company) are rejected."""
    # Missing company
    rec, is_valid, errors = normalize_client_row({
        "Tenant Company Name": "",
        "Primary Mobile Number": "8287511886",
    })
    assert is_valid is False
    assert any("company" in e.lower() for e in errors)

    # Missing mobile
    rec2, is_valid2, errors2 = normalize_client_row({
        "Tenant Company Name": "Good Earth Infra",
        "Primary Mobile Number": "",
    })
    assert is_valid2 is False
    assert any("mobile" in e.lower() for e in errors2)


def test_newly_added_client_recognized_via_on_demand_sync(monkeypatch, mock_supabase):
    """
    Verifies that when a client is added to the Google Sheet after server startup,
    the lookup detects the cache miss, triggers an on-demand refresh, and successfully
    identifies the new client without hardcoding.
    """
    # Initially, cache does NOT have Safwan
    stale_cache = [
        {
            "building": "GEBB1",
            "company_name": "HDFC Bank",
            "floor": "Ground Floor",
            "unit_number": "R1/R2",
            "admin_name": "Rakesh Kumar",
            "designation": "Admin",
            "mobile_number": "918826896085",
            "is_active": True,
        }
    ]

    # After on-demand sheet refresh, cache includes Safwan
    refreshed_cache = list(stale_cache) + [
        {
            "building": "GEBB2",
            "company_name": "Good Earth Infra",
            "floor": "Ground Floor",
            "unit_number": "GEEBTWOTest",
            "admin_name": "safwan",
            "designation": "Admin",
            "mobile_number": "918287511886",
            "is_active": True,
        }
    ]

    monkeypatch.setattr("clients.sync_engine.get_cached_clients", lambda: stale_cache)

    sync_called = False
    def mock_on_demand_refresh():
        nonlocal sync_called
        sync_called = True
        return refreshed_cache

    monkeypatch.setattr("clients.sync_engine.maybe_refresh_client_cache_on_miss", mock_on_demand_refresh)

    matches = lookup_clients_by_phone("+91 82875 11886")
    assert sync_called is True
    assert len(matches) == 1
    assert matches[0]["admin_name"] == "safwan"
    assert matches[0]["company_name"] == "Good Earth Infra"


# ── Full WhatsApp Greeting & Menu Delivery Verification ───────────────────────

def test_safwan_whatsapp_welcome_menu_and_details(monkeypatch, mock_supabase):
    """
    Verifies the end-to-end client greeting and menu delivery for Safwan:
    - Personalized greeting with 'safwan'
    - Company: 'Good Earth Infra'
    - Building: 'Business Bay-II'
    - Unit: 'GEEBTWOTest'
    - Menu options (Log New Complaint, Select Complaints, Complaint History, Feedback)
    """
    safwan_rec = {
        "building": "GEBB2",
        "company_name": "Good Earth Infra",
        "floor": "Ground Floor",
        "unit_number": "GEEBTWOTest",
        "admin_name": "safwan",
        "designation": "Admin",
        "mobile_number": "918287511886",
        "email": "",
        "factech_client_id": None,
        "is_active": True,
        "source_sheet": "MASTER",
        "sync_key": "safwan_sync_key",
    }
    monkeypatch.setattr("clients.sync_engine.get_cached_clients", lambda: [safwan_rec])

    with patch("clients.flows.send_list_message") as mock_list, patch("clients.flows.send_text") as mock_text:
        # Simulate send_list_message succeeding
        mock_list.return_value = True

        handle_client_hi("918287511886")

        assert mock_list.called
        call_args = mock_list.call_args[0]
        recipient = call_args[0]
        body = call_args[1]
        button_label = call_args[2]
        sections = call_args[3]

        assert recipient == "918287511886"
        assert "Hi safwan 👋" in body
        assert "Good Earth Infra" in body
        assert "Business Bay-II" in body
        assert "GEEBTWOTest" in body

        # Verify menu options
        row_ids = [r["id"] for r in sections[0]["rows"]]
        assert "log_new_complaint" in row_ids
        assert "select_complaints" in row_ids
        assert "complaint_history" in row_ids
        assert "feedback_closed_complaints" in row_ids


def test_safwan_active_client_context(monkeypatch, mock_supabase):
    """Verifies get_active_client_context returns Safwan's unit and company."""
    safwan_rec = {
        "building": "GEBB2",
        "company_name": "Good Earth Infra",
        "floor": "Ground Floor",
        "unit_number": "GEEBTWOTest",
        "admin_name": "safwan",
        "designation": "Admin",
        "mobile_number": "918287511886",
        "email": "",
        "factech_client_id": None,
        "is_active": True,
        "source_sheet": "MASTER",
        "sync_key": "safwan_sync_key",
    }
    monkeypatch.setattr("clients.sync_engine.get_cached_clients", lambda: [safwan_rec])

    ctx = get_active_client_context("+91 82875 11886")
    assert ctx is not None
    assert ctx["admin_name"] == "safwan"
    assert ctx["company_name"] == "Good Earth Infra"
    assert ctx["building"] == "Business Bay-II"
    assert ctx["unit_number"] == "GEEBTWOTest"


def test_get_client_by_id_with_cached_client(monkeypatch):
    """Verifies that get_client_by_id resolves client by ID or sync_key without NameError."""
    from clients.service import get_client_by_id
    safwan_rec = {
        "id": "safwan_id_123",
        "mobile_number": "918287511886",
        "company_name": "Good Earth Infra",
        "building": "Business Bay-II",
        "unit_number": "GEEBTWOTest",
        "contact_name": "safwan",
        "is_active": True,
        "sync_key": "safwan_sync_key",
    }
    monkeypatch.setattr("clients.sync_engine.get_cached_clients", lambda: [safwan_rec])

    # 1. Lookup by id
    client_by_id = get_client_by_id("safwan_id_123")
    assert client_by_id is not None
    assert client_by_id["contact_name"] == "safwan"

    # 2. Lookup by sync_key
    client_by_key = get_client_by_id("safwan_sync_key")
    assert client_by_key is not None
    assert client_by_key["company_name"] == "Good Earth Infra"

