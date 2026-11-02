from __future__ import annotations

"""
Facilities Module — Flow Router
==================================
Central dispatcher for all Facilities conversational flows.

Routing logic:
  1. Check if user has an active facilities_session with a flow state
     → Route to the in-progress flow handler
  2. Check if the message is an interactive button/list reply
     → Route by button/list ID prefix
  3. For free text: extract intent via LLM
     → Route to the appropriate flow handler
  4. Fallback: show the Facilities home menu
"""

import logging
import json
from datetime import datetime, timezone

from db import supabase
from whatsapp.ux import clean_phone_number
from elara.config import KANAV_PHONE, DEVELOPER_PHONE, KANAV_PHONES
from facilities.config import BUILDING_TABS
from facilities.auth import resolve_facilities_user, is_all_buildings_user

logger = logging.getLogger(__name__)


# ── Session Management ───────────────────────────────────────────────────────

def get_session(whatsapp_number: str) -> dict | None:
    """Get the current Facilities session for a user."""
    try:
        res = supabase.table("facilities_sessions").select("*").eq(
            "whatsapp_number", whatsapp_number
        ).execute()
        if res and isinstance(res.data, list) and len(res.data) > 0 and isinstance(res.data[0], dict):
            session = dict(res.data[0])
            # Parse JSON fields
            if isinstance(session.get("draft_task_json"), str):
                try:
                    session["draft_task_json"] = json.loads(session["draft_task_json"])
                except Exception:
                    session["draft_task_json"] = {}
            if isinstance(session.get("context_json"), str):
                try:
                    session["context_json"] = json.loads(session["context_json"])
                except Exception:
                    session["context_json"] = {}
            return session
        return None
    except Exception as e:
        logger.error(f"get_session failed for {whatsapp_number}: {e}")
        return None


def set_session(whatsapp_number: str, flow_state: str,
                draft: dict | None = None, context: dict | None = None):
    """Create or update a Facilities session."""
    try:
        payload = {
            "whatsapp_number": whatsapp_number,
            "current_flow_state": flow_state,
            "last_active_at": datetime.now(timezone.utc).isoformat(),
        }
        if draft is not None:
            payload["draft_task_json"] = json.dumps(draft)
        if context is not None:
            payload["context_json"] = json.dumps(context)

        supabase.table("facilities_sessions").upsert(
            payload, on_conflict="whatsapp_number"
        ).execute()
    except Exception as e:
        logger.error(f"set_session failed for {whatsapp_number}: {e}")


def clear_session(whatsapp_number: str):
    """Clear/delete the Facilities session for a user."""
    try:
        supabase.table("facilities_sessions").delete().eq(
            "whatsapp_number", whatsapp_number
        ).execute()
    except Exception as e:
        logger.error(f"clear_session failed for {whatsapp_number}: {e}")


# ── Main Router ──────────────────────────────────────────────────────────────

def route_facilities_message(sender: str, text: str | None = None,
                             button_id: str | None = None, user: dict | None = None,
                             image_data: dict | None = None,
                             voice_transcript: str | None = None):
    """
    Main entry point for all Facilities module messages.

    Args:
        sender: WhatsApp number
        text: Plain text message (if any)
        button_id: Interactive button/list reply ID (if any)
        user: Authenticated user dict
        image_data: Image attachment data (if any)
        voice_transcript: Transcribed voice note text (if any)
    """
    clean_sender = clean_phone_number(sender)
    fac_user = resolve_facilities_user(clean_sender or sender)

    is_admin = (
        clean_sender in KANAV_PHONES
        or clean_sender in (KANAV_PHONE, DEVELOPER_PHONE)
        or (user and (
            str(user.get("name", "")).strip().lower() in ("kanav", "developer", "anoop", "abhijeet")
            or str(user.get("role", "")).strip().lower() in ("director", "developer", "facility head", "facilities head")
        ))
    )

    if not fac_user or not fac_user.get("is_facilities_user"):
        if is_admin:
            fac_user = dict(user) if user else {
                "name": "Kanav",
                "role": "Director",
                "department": "Facilities",
                "whatsapp_number": clean_sender,
            }
            fac_user["is_facilities_user"] = True
            fac_user["permitted_buildings"] = BUILDING_TABS.copy()
        elif user and (
            str(user.get("role", "")).strip().lower() in ("director", "developer", "facility head", "facilities head")
            or str(user.get("department", "")).strip().lower() == "facilities"
            or bool(user.get("permitted_buildings"))
            or user.get("is_facilities_user")
            or is_all_buildings_user(user)
        ):
            fac_user = dict(user)
            fac_user["is_facilities_user"] = True
            if not fac_user.get("permitted_buildings"):
                fac_user["permitted_buildings"] = BUILDING_TABS.copy()

    if not fac_user or not fac_user.get("is_facilities_user"):
        # Not a Facilities user — shouldn't reach here, but handle gracefully
        from whatsapp.ux import send_text
        send_text(sender, "You are not registered as a Facilities team member. Please contact your administrator.")
        return
    user = fac_user

    # Update session timestamp
    session = get_session(sender) or {}

    # 1. Handle interactive button/list replies
    if button_id:
        _route_button(sender, button_id, user, session)
        return

    # 2. Handle image attachments
    if image_data:
        _route_image(sender, image_data, user, session)
        return

    # 3. Handle voice transcriptions
    if voice_transcript:
        _route_voice(sender, voice_transcript, user, session)
        return

    # 4. Handle text messages
    if text:
        _route_text(sender, text, user, session)
        return


def _route_button(sender: str, button_id: str, user: dict, session: dict):
    """Route interactive button/list reply IDs."""
    logger.info(f"Facilities button route: {button_id}")

    # ── Home Menu Actions ────────────────────────────────────────────────
    if button_id == "fac_my_tasks":
        from facilities.flows.my_tasks import show_my_tasks
        show_my_tasks(sender, user)

    elif button_id == "fac_team_tasks":
        from facilities.flows.building_filter import prompt_building_filter
        prompt_building_filter(sender, user, next_action="team_tasks")

    elif button_id == "fac_completed_tasks":
        from facilities.flows.completed_tasks import prompt_completed_tasks_building
        prompt_completed_tasks_building(sender, user)

    elif button_id == "fac_create_task":
        from facilities.flows.create_task import start_create_flow
        start_create_flow(sender, user)

    elif button_id == "fac_summary":
        from facilities.flows.summary import show_summary
        show_summary(sender, user)

    elif button_id == "fac_sync_status":
        from facilities.flows.sync_status import show_sync_status
        show_sync_status(sender, user)

    elif button_id in ("fac_eod_report", "fac_report", "fac_pdf_report"):
        from facilities.eod_report import send_facilities_eod_report
        from whatsapp.ux import send_text
        send_text(sender, "📄 *Generating Facilities EOD Report...*\nPlease wait a moment while your report is generated.")
        send_facilities_eod_report(sender, send_summary_text=True)

    # ── Building Filter Selection ────────────────────────────────────────
    elif button_id.startswith("fac_bldg_"):
        building = button_id.replace("fac_bldg_", "")
        _handle_building_selection(sender, building, user, session)

    # ── Task Card Actions ────────────────────────────────────────────────
    elif button_id.startswith("fac_view_"):
        ref_no = button_id.replace("fac_view_", "")
        from facilities.flows.task_card import show_task_card
        show_task_card(sender, ref_no, user)

    elif button_id == "fac_update_task":
        from facilities.flows.my_tasks import show_my_tasks
        from whatsapp.ux import send_text
        send_text(sender, "🔄 *Update Task*\nPlease select a task below to update its status or progress:")
        show_my_tasks(sender, user)

    elif button_id.startswith("fac_update_"):
        ref_no = button_id.replace("fac_update_", "")
        from facilities.flows.update_task import start_update_flow
        start_update_flow(sender, ref_no, user)

    elif button_id.startswith("fac_reassign_"):
        ref_no = button_id.replace("fac_reassign_", "")
        from facilities.flows.task_card import start_reassign_flow
        start_reassign_flow(sender, ref_no, user)

    elif button_id.startswith("fac_attach_"):
        ref_no = button_id.replace("fac_attach_", "")
        from facilities.flows.task_card import prompt_attachment
        prompt_attachment(sender, ref_no, user)

    elif button_id.startswith("fac_history_"):
        ref_no = button_id.replace("fac_history_", "")
        from facilities.flows.history import show_history
        show_history(sender, ref_no, user)

    # ── Task Creation Flow ───────────────────────────────────────────────
    elif button_id.startswith("fac_type_"):
        task_type = button_id.replace("fac_type_", "")
        from facilities.flows.create_task import handle_type_selection
        handle_type_selection(sender, task_type, user)

    elif button_id == "fac_confirm_create":
        from facilities.flows.create_task import confirm_create
        confirm_create(sender, user)

    elif button_id == "fac_edit_draft":
        from facilities.flows.create_task import edit_draft
        edit_draft(sender, user)

    elif button_id == "fac_cancel_create":
        from facilities.flows.create_task import cancel_create
        cancel_create(sender, user)

    # ── Duplicate Detection ──────────────────────────────────────────────
    elif button_id.startswith("fac_update_existing_"):
        ref_no = button_id.replace("fac_update_existing_", "")
        from facilities.flows.update_task import start_update_flow
        start_update_flow(sender, ref_no, user)

    elif button_id == "fac_create_anyway":
        from facilities.flows.create_task import confirm_create
        confirm_create(sender, user, skip_duplicate_check=True)

    # ── Status Validation ────────────────────────────────────────────────
    elif button_id == "fac_confirm_update":
        from facilities.flows.update_task import confirm_update
        confirm_update(sender, user)

    elif button_id == "fac_cancel_update":
        clear_session(sender)
        from whatsapp.ux import send_text as _st
        _st(sender, "❌ Update cancelled.\n\n_Type *menu* to go back._")

    elif button_id.startswith("fac_status_"):
        new_status = button_id.replace("fac_status_", "")
        from facilities.flows.update_task import handle_status_selection
        handle_status_selection(sender, new_status, user)

    elif button_id == "fac_reopen_confirm":
        from facilities.flows.status_validation import confirm_reopen
        confirm_reopen(sender, user)

    elif button_id == "fac_reopen_cancel":
        from facilities.flows.status_validation import cancel_reopen
        cancel_reopen(sender, user)

    # ── Conflict Resolution ──────────────────────────────────────────────
    elif button_id.startswith("fac_keep_sheet_"):
        conflict_id = button_id.replace("fac_keep_sheet_", "")
        from facilities.flows.conflict_ui import resolve_keep_sheet
        resolve_keep_sheet(sender, conflict_id, user)

    elif button_id.startswith("fac_keep_bot_"):
        conflict_id = button_id.replace("fac_keep_bot_", "")
        from facilities.flows.conflict_ui import resolve_keep_bot
        resolve_keep_bot(sender, conflict_id, user)

    elif button_id.startswith("fac_conflict_history_"):
        ref_no = button_id.replace("fac_conflict_history_", "")
        from facilities.flows.history import show_history
        show_history(sender, ref_no, user)

    # ── Reassign Flow ────────────────────────────────────────────────────
    elif button_id.startswith("fac_assignto_"):
        new_owner = button_id.replace("fac_assignto_", "")
        from facilities.flows.task_card import handle_reassign_selection
        handle_reassign_selection(sender, new_owner, user)

    # ── Voice Confirmation ───────────────────────────────────────────────
    elif button_id == "fac_voice_confirm_all":
        from facilities.flows.voice_handler import confirm_all_voice_ops
        confirm_all_voice_ops(sender, user)

    elif button_id == "fac_voice_cancel":
        from facilities.flows.voice_handler import cancel_voice_ops
        cancel_voice_ops(sender, user)

    # ── Voice Fallback Actions ───────────────────────────────────────────
    elif button_id == "fac_voice_create_task":
        from facilities.flows.voice_handler import handle_voice_fallback_create
        handle_voice_fallback_create(sender, user)

    elif button_id == "fac_voice_update_task":
        from facilities.flows.voice_handler import handle_voice_fallback_update
        handle_voice_fallback_update(sender, user)

    elif button_id == "fac_voice_discard":
        from facilities.flows.voice_handler import handle_voice_fallback_discard
        handle_voice_fallback_discard(sender, user)

    # ── Back to Home ─────────────────────────────────────────────────────
    elif button_id == "fac_home":
        from facilities.flows.home import show_home
        show_home(sender, user)

    # ── View All Tasks (pagination) ──────────────────────────────────────
    elif button_id.startswith("fac_viewall_"):
        parts = button_id.replace("fac_viewall_", "").split("_", 1)
        if len(parts) == 2:
            building, task_type = parts
            from facilities.flows.my_tasks import show_tasks_by_type
            show_tasks_by_type(sender, user, building, task_type)

    # ── Daily Digest ─────────────────────────────────────────────────────
    elif button_id == "fac_daily_digest":
        from facilities.flows.daily_digest import show_daily_digest
        show_daily_digest(sender, user)

    # ── Completed Tasks ──────────────────────────────────────────────────
    elif button_id.startswith("fac_completed_bldg_"):
        bldg = button_id.replace("fac_completed_bldg_", "")
        from facilities.flows.completed_tasks import show_completed_tasks
        show_completed_tasks(sender, user, bldg)

    # ── Overdue Tasks ────────────────────────────────────────────────────
    elif button_id == "fac_overdue_tasks":
        from facilities.flows.overdue_tasks import handle_overdue_request
        handle_overdue_request(sender, user)

    elif button_id.startswith("fac_overdue_bldg_"):
        bldg = button_id.replace("fac_overdue_bldg_", "")
        from facilities.flows.overdue_tasks import show_overdue_tasks
        show_overdue_tasks(sender, user, bldg)

    else:
        logger.warning(f"Unknown Facilities button ID: {button_id}")
        from whatsapp.ux import send_text
        send_text(sender, "Sorry, I didn't understand that action. Let me show you the menu.")
        from facilities.flows.home import show_home
        show_home(sender, user)


def _try_parse_direct_task_update(text: str) -> dict | None:
    """Check if the text is a direct task status or note update command with a Ref No."""
    import re
    clean = text.strip()

    # Match Pattern 1: (mark/set/update) REF (as/to) STATUS (: note)
    m = re.search(
        r'\b(?:mark|set|update|change)?\s*(GEBB1|GEBB2|GETT|COM|COMMON)[-\s]?(\d{1,4})\s+(?:as\s+|to\s+|status\s+to\s+)?(open|wip|in\s*progress|closed|complete|completed|done|on\s*hold|escalate|escalated)\b(?:\s*[:\-–—,]\s*(.*))?',
        clean,
        re.IGNORECASE
    )
    if m:
        bldg_code = m.group(1).upper()
        if bldg_code == "COMMON":
            bldg_code = "COM"
        num = int(m.group(2))
        ref_no = f"{bldg_code}-{num:03d}" if bldg_code != "COM" else f"COM-{num:03d}"
        raw_status = m.group(3).lower()
        note = m.group(4).strip() if m.group(4) else ""

        status_map = {
            "open": "Open", "wip": "WIP", "in progress": "WIP", "inprogress": "WIP",
            "closed": "Closed", "complete": "Closed", "completed": "Closed", "done": "Closed",
            "on hold": "On Hold", "onhold": "On Hold", "escalate": "Open", "escalated": "Open",
        }
        status = status_map.get(raw_status, "Open")
        return {"ref_no": ref_no, "status": status, "latest_update": note}

    # Match Pattern 2: (close/reopen/complete) REF (: note)
    m_verb = re.search(
        r'\b(close|reopen|complete)\s+(GEBB1|GEBB2|GETT|COM|COMMON)[-\s]?(\d{1,4})\b(?:\s*[:\-–—,]\s*(.*))?',
        clean,
        re.IGNORECASE
    )
    if m_verb:
        verb = m_verb.group(1).lower()
        bldg_code = m_verb.group(2).upper()
        if bldg_code == "COMMON":
            bldg_code = "COM"
        num = int(m_verb.group(3))
        ref_no = f"{bldg_code}-{num:03d}" if bldg_code != "COM" else f"COM-{num:03d}"
        note = m_verb.group(4).strip() if m_verb.group(4) else ""
        status = "Closed" if verb in ("close", "complete") else "Open"
        return {"ref_no": ref_no, "status": status, "latest_update": note}

    return None


def _route_text(sender: str, text: str, user: dict, session: dict):
    """Route free-text messages."""
    clean = text.strip().lower()

    # 1. Check for greeting/menu/cancel triggers
    if clean in ("hi", "hello", "hey", "menu", "start", "home", "cancel", "reset", "exit"):
        clear_session(sender)
        from facilities.flows.home import show_home
        show_home(sender, user)
        return

    # 2. Direct Task Update (e.g. "Mark GETT-013 as closed", "Close GETT-006") - 0-latency instant match
    direct_update = _try_parse_direct_task_update(text)
    if direct_update:
        from facilities.flows.update_task import start_update_flow
        start_update_flow(sender, direct_update["ref_no"], user, prefill=direct_update)
        return

    # 3. Check if user is in an active text-entry multi-step flow state
    state = session.get("current_flow_state", "") if session else ""
    ACTIVE_INPUT_STATES = (
        "create_issue", "create_target_date", "create_owner", "create_preview",
        "update_note", "update_expected_date", "update_ref_no_input", "update_confirm", "update_reopen_confirm",
        "reassign_user_input", "voice_confirm", "voice_fallback",
    )
    if state in ACTIVE_INPUT_STATES:
        _route_in_flow_text(sender, text, user, session)
        return

    # If in selection / filter states:
    if state in ("create_building", "create_type", "update_status",
                 "building_filter", "completed_tasks_building", "overdue_tasks_building"):
        # If user typed an explicit top-level command, break out of the selection state
        if any(clean.startswith(p) for p in ("create ", "new task", "add task", "raise task", "update ", "show ", "view ", "mark ", "close ")) or _is_task_filter_query(clean):
            clear_session(sender)
            # Proceed to top-level intent/filter routing below
        else:
            # Route in-flow (e.g. typing building name, typing type name, etc.)
            _route_in_flow_text(sender, text, user, session)
            return

    # 2.5 Check for "my tasks" triggers
    if clean in ("my tasks", "my task", "show my tasks", "view my tasks", "show my task", "view my task", "tasks assigned to me", "my assigned tasks"):
        from facilities.flows.my_tasks import show_my_tasks
        show_my_tasks(sender, user)
        return

    # 3. Check for completed tasks triggers
    if any(k in clean for k in ("completed task", "completed tasks", "closed task", "closed tasks", "show completed", "show closed")):
        from facilities.auth import fuzzy_match_building
        matched_bldg = fuzzy_match_building(text)
        if matched_bldg:
            from facilities.flows.completed_tasks import show_completed_tasks
            show_completed_tasks(sender, user, matched_bldg)
        elif "all" in clean:
            from facilities.flows.completed_tasks import show_completed_tasks
            show_completed_tasks(sender, user, "all")
        else:
            from facilities.flows.completed_tasks import prompt_completed_tasks_building
            prompt_completed_tasks_building(sender, user)
        return

    # 3.5 Check for Facilities EOD Report triggers (e.g. "Facilities Report", "Facilitite Report", "EOD Report")
    FACILITIES_REPORT_TRIGGERS = (
        "facilities report", "facilitite report", "facility report", "facilite report",
        "facilities eod", "facility eod", "facilities eod report", "facility eod report",
        "eod report", "pdf report", "download report", "send report", "get report",
        "facilities pdf", "facility pdf", "daily eod report", "facilities eod pdf",
    )
    if any(t in clean for t in FACILITIES_REPORT_TRIGGERS) or (clean in ("report", "eod", "pdf")):
        from facilities.eod_report import send_facilities_eod_report
        from whatsapp.ux import send_text
        send_text(sender, "📄 *Generating Facilities EOD Report...*\nPlease wait a moment while your report is generated.")
        send_facilities_eod_report(sender, send_summary_text=True)
        return

    # 4. Check for overdue tasks triggers (rule-based, before LLM)
    if any(k in clean for k in ("overdue", "over due", "past due", "show overdue")):
        from facilities.flows.overdue_tasks import handle_overdue_request
        from facilities.auth import fuzzy_match_building
        matched_bldg = fuzzy_match_building(text)
        handle_overdue_request(sender, user, building=matched_bldg)
        return

    # 5. Check for task filter queries (employee names, status, future)
    if _is_task_filter_query(clean):
        from facilities.task_filter import parse_filter_from_text, TaskFilterCriteria
        from facilities.flows.overdue_tasks import show_filtered_tasks
        criteria = parse_filter_from_text(text, user)
        # If we got meaningful criteria, run the filter
        if (criteria.employee_name or criteria.status or criteria.overdue
                or criteria.future or criteria.date_range_start):
            show_filtered_tasks(sender, user, criteria)
            return

    # 6. Extract intent via LLM
    from facilities.llm import extract_intent
    result = extract_intent(text)

    if not result.get("intents"):
        from facilities.flows.home import show_home
        show_home(sender, user)
        return

    primary = result["intents"][0]
    intent = primary.get("intent")
    entities = primary.get("entities", {})
    confidence = primary.get("confidence", 0)

    logger.info(f"Facilities intent: {intent} (confidence: {confidence}) for {sender}")

    if intent == "greeting":
        from facilities.flows.home import show_home
        show_home(sender, user)

    elif intent == "view_my_tasks":
        from facilities.flows.my_tasks import show_my_tasks
        show_my_tasks(sender, user)

    elif intent == "view_team_tasks":
        building = entities.get("building")
        if building:
            from facilities.flows.team_tasks import show_team_tasks
            show_team_tasks(sender, building, user)
        else:
            from facilities.flows.building_filter import prompt_building_filter
            prompt_building_filter(sender, user, next_action="team_tasks")

    elif intent == "create_task":
        from facilities.flows.create_task import start_create_flow
        start_create_flow(sender, user, prefill=entities)

    elif intent == "update_task":
        ref_no = entities.get("ref_no")
        if ref_no:
            from facilities.flows.update_task import start_update_flow
            start_update_flow(sender, ref_no, user, prefill=entities)
        else:
            from facilities.flows.update_task import prompt_ref_no_for_update
            prompt_ref_no_for_update(sender, user)

    elif intent == "view_task_detail":
        ref_no = entities.get("ref_no")
        if ref_no:
            from facilities.flows.task_card import show_task_card
            show_task_card(sender, ref_no, user)
        else:
            from whatsapp.ux import send_text
            send_text(sender, "Please provide the task Ref No (e.g., GEBB1-001).")

    elif intent == "view_summary":
        from facilities.flows.summary import show_summary
        show_summary(sender, user)

    elif intent == "view_sync_status":
        from facilities.flows.sync_status import show_sync_status
        show_sync_status(sender, user)

    elif intent == "view_history":
        ref_no = entities.get("ref_no")
        if ref_no:
            from facilities.flows.history import show_history
            show_history(sender, ref_no, user)
        else:
            from whatsapp.ux import send_text
            send_text(sender, "Please provide the task Ref No to view history (e.g., GEBB1-001).")

    elif intent == "show_overdue_tasks":
        building = entities.get("building")
        from facilities.flows.overdue_tasks import handle_overdue_request
        handle_overdue_request(sender, user, building=building)

    elif intent == "filter_tasks":
        from facilities.task_filter import TaskFilterCriteria
        from facilities.flows.overdue_tasks import show_filtered_tasks
        criteria = TaskFilterCriteria(
            building=entities.get("building"),
            employee_name=entities.get("employee_name"),
            status=entities.get("status"),
            overdue=bool(entities.get("overdue")),
            future=bool(entities.get("future")),
            date_range_start=entities.get("date_range_start"),
            date_range_end=entities.get("date_range_end"),
            date_field=entities.get("date_field", "target_date"),
        )
        show_filtered_tasks(sender, user, criteria)

    elif intent == "daily_digest":
        from facilities.flows.daily_digest import show_daily_digest
        show_daily_digest(sender, user)

    elif intent == "help":
        _show_help(sender)

    else:
        from facilities.flows.home import show_home
        show_home(sender, user)


def _route_in_flow_text(sender: str, text: str, user: dict, session: dict):
    """Route text when user is in a multi-step flow."""
    state = session.get("current_flow_state", "") if session else ""
    clean = text.strip().lower()

    # If the user wants to cancel or return to menu
    if clean in ("cancel", "exit", "quit", "menu", "home", "start", "reset", "clear"):
        clear_session(sender)
        from facilities.flows.home import show_home
        show_home(sender, user)
        return

    # If user sent a top-level command (e.g. "create task...", "show overdue...", "update..."),
    # break out of selection/filter states to process the new command directly
    if state in ("building_filter", "completed_tasks_building", "overdue_tasks_building", "create_building"):
        if any(clean.startswith(p) for p in ("create ", "new task", "add task", "raise task", "update ", "show ", "view ")) or _is_task_filter_query(clean):
            clear_session(sender)
            _route_text(sender, text, user, {})
            return

    # Create task flow states
    if state.startswith("create_"):
        from facilities.flows.create_task import handle_create_flow_text
        handle_create_flow_text(sender, text, user, session)

    # Update task flow states
    elif state.startswith("update_"):
        from facilities.flows.update_task import handle_update_flow_text
        handle_update_flow_text(sender, text, user, session)

    # Building filter
    elif state == "building_filter":
        from facilities.flows.building_filter import handle_building_text
        handle_building_text(sender, text, user, session)

    # Completed tasks building filter
    elif state == "completed_tasks_building":
        from facilities.auth import fuzzy_match_building
        bldg = fuzzy_match_building(text)
        if bldg:
            from facilities.flows.completed_tasks import show_completed_tasks
            show_completed_tasks(sender, user, bldg)
        elif text.strip().lower() in ("all", "all buildings", "every building"):
            from facilities.flows.completed_tasks import show_completed_tasks
            show_completed_tasks(sender, user, "all")
        else:
            from facilities.flows.completed_tasks import prompt_completed_tasks_building
            prompt_completed_tasks_building(sender, user)

    # Overdue tasks building filter
    elif state == "overdue_tasks_building":
        from facilities.auth import fuzzy_match_building
        bldg = fuzzy_match_building(text)
        if bldg:
            from facilities.flows.overdue_tasks import show_overdue_tasks
            show_overdue_tasks(sender, user, bldg)
        else:
            from whatsapp.ux import send_text as _st
            _st(sender, f"I couldn't find a building matching \"{text}\". Please select from the list.")
            from facilities.flows.overdue_tasks import prompt_overdue_building_selection
            prompt_overdue_building_selection(sender, user)

    # Reassign flow
    elif state.startswith("reassign_"):
        from facilities.flows.task_card import handle_reassign_text
        handle_reassign_text(sender, text, user, session)

    # Voice confirmation / building selection / fallback
    elif state in ("voice_confirm", "voice_awaiting_building", "voice_fallback"):
        from facilities.flows.voice_handler import handle_voice_confirm_text
        handle_voice_confirm_text(sender, text, user, session)

    else:
        # Unknown state — clear and show home
        clear_session(sender)
        from facilities.flows.home import show_home
        show_home(sender, user)


def _route_image(sender: str, image_data: dict, user: dict, session: dict):
    """Route image attachments."""
    if session and session.get("current_flow_state", "").startswith("attach_"):
        ref_no = session.get("context_json", {}).get("ref_no")
        if ref_no:
            from facilities.flows.task_card import handle_attachment_upload
            handle_attachment_upload(sender, ref_no, image_data, user)
            return

    from whatsapp.ux import send_text
    send_text(sender, "To attach an image to a task, first open the task card and tap *Attach*.")


def _is_navigational_voice_command(transcript: str) -> bool:
    """Detect if a voice transcript is a navigational/query command.

    Navigational commands (e.g. "show my tasks", "overdue tasks", "menu")
    should be routed through ``_route_text`` which already handles them.
    Data-bearing transcripts (task descriptions, progress updates) should
    go through the voice operations extraction pipeline instead.
    """
    import re
    clean = transcript.strip().lower()

    # ── Greetings / menu / cancel ────────────────────────────────────────
    if clean in (
        "hi", "hello", "hey", "menu", "start", "home", "cancel",
        "reset", "exit", "help", "back",
    ):
        return True

    # ── "my tasks" variants ──────────────────────────────────────────────
    MY_TASKS_PHRASES = (
        "my tasks", "my task", "show my tasks", "show my task",
        "view my tasks", "view my task", "tasks assigned to me",
        "my assigned tasks",
    )
    if clean in MY_TASKS_PHRASES or any(p in clean for p in MY_TASKS_PHRASES):
        return True

    # ── "show/view/list tasks" (optionally with building) ────────────────
    if re.search(
        r'\b(show|view|list|get|display|see)\b.*\btasks?\b', clean
    ):
        return True

    # ── "team tasks" ─────────────────────────────────────────────────────
    if "team task" in clean:
        return True

    # ── Completed / closed tasks ─────────────────────────────────────────
    if any(p in clean for p in (
        "completed task", "closed task", "show completed", "show closed",
    )):
        return True

    # ── Overdue tasks ────────────────────────────────────────────────────
    if any(p in clean for p in ("overdue", "over due", "past due")):
        return True

    # ── Create / new / add / raise task (command form) ───────────────────
    if any(clean.startswith(p) for p in (
        "create task", "create a task", "new task", "add task",
        "raise task", "raise a task",
    )):
        return True

    # ── Direct task update with ref no (mark/close/update GETT-013) ─────
    if re.search(
        r'\b(update|mark|set|close|reopen|complete|change)\b.*'
        r'\b(GEBB1|GEBB2|GETT|COM|COMMON)[-\s]?\d',
        clean, re.IGNORECASE,
    ):
        return True

    # Also match "GETT-013 close" / "GEBB1 042 as WIP" (ref before verb)
    if _try_parse_direct_task_update(transcript):
        return True

    # ── Summary ──────────────────────────────────────────────────────────
    if any(p in clean for p in ("summary", "show summary", "view summary")):
        return True

    # ── Report / EOD ─────────────────────────────────────────────────────
    REPORT_PHRASES = (
        "report", "eod report", "eod", "pdf report",
        "facilities report", "facility report", "download report",
        "send report", "get report",
    )
    if clean in REPORT_PHRASES or any(p in clean for p in REPORT_PHRASES):
        return True

    # ── Sync status ──────────────────────────────────────────────────────
    if any(p in clean for p in ("sync status", "sync health", "sheet sync")):
        return True

    # ── Daily digest ─────────────────────────────────────────────────────
    if any(p in clean for p in ("daily digest", "daily update")):
        return True

    # ── History ──────────────────────────────────────────────────────────
    if "history" in clean and re.search(
        r'\b(GEBB1|GEBB2|GETT|COM|COMMON)[-\s]?\d', clean, re.IGNORECASE
    ):
        return True

    return False


def _route_voice(sender: str, transcript: str, user: dict, session: dict):
    """Route voice note transcriptions.

    Two-phase routing:
      1. If the transcript is a navigational/query command (e.g. "show my
         tasks", "overdue tasks", "menu"), route through ``_route_text``
         which already handles all these intents correctly.
      2. Otherwise, send to the voice operations handler for LLM-based
         extraction of task mutations (create/update/reassign).
    """
    from facilities.alias_normalizer import normalize_building_aliases
    transcript = normalize_building_aliases(transcript)

    # Phase 1: Navigational commands → reuse existing text routing
    if _is_navigational_voice_command(transcript):
        logger.info(f"Voice transcript is navigational command, routing as text: {transcript[:80]}")
        _route_text(sender, transcript, user, session)
        return

    # Phase 2: Data-bearing voice notes → voice operations extraction
    from facilities.flows.voice_handler import handle_voice_note
    handle_voice_note(sender, transcript, user)


def _is_task_filter_query(clean: str) -> bool:
    """Check if free text is a task filter query."""
    import re
    # Never treat creation, update, or report phrases as filter queries!
    if any(clean.startswith(p) for p in ("create ", "new ", "add ", "raise ", "update ", "change ", "edit ", "reassign ", "attach ", "mark ", "close ", "set ", "reopen ")) or any(w in clean for w in ("report", "eod", "pdf")):
        return False

    # If message contains a specific task Ref No (e.g. GETT-013, GEBB1-002), it is an update or view, not a filter query!
    if re.search(r'\b(GEBB1|GEBB2|GETT|COM|COMMON)[-\s]?\d{1,4}\b', clean, re.IGNORECASE):
        return False

    has_filter_keyword = any(w in clean for w in ["task", "tasks", "show", "view", "list", "what", "which", "filter", "find", "get", "assigned"])

    # Check for employee names with a filter keyword or possessive
    try:
        from facilities.owner_resolver import match_employee_name
        for word in clean.split():
            clean_word = word.strip(".,'\"?!:;").lower()
            if clean_word.endswith("'s"):
                clean_word = clean_word[:-2]
                if match_employee_name(clean_word) and (has_filter_keyword or "task" in clean):
                    return True
            if match_employee_name(clean_word) and has_filter_keyword:
                # e.g. "show Vikash's tasks", "Vikash tasks", "tasks for Vikash"
                if any(p in clean for p in ["created by", "done by", "made by", "tested by", "reported by"]):
                    return False
                return True
    except Exception:
        pass

    # Check for status queries with filter keyword
    status_keywords = ["pending", "open", "wip", "in progress", "in-progress", "closed", "completed", "done", "on hold", "on-hold", "escalated", "blocked", "future", "upcoming"]
    if any(k in clean for k in status_keywords) and has_filter_keyword:
        return True

    # Check for date queries
    if any(w in clean for w in ["between", "due this week", "due next week", "raised between", "due between", "tasks due", "tasks raised"]):
        return True

    return False


def _handle_building_selection(sender: str, building: str, user: dict, session: dict):
    """Handle a building selection from the filter or flow."""
    state = session.get("current_flow_state", "") if session else ""
    next_action = None
    if session and session.get("context_json"):
        next_action = session["context_json"].get("next_action")

    if state == "voice_awaiting_building":
        from facilities.flows.voice_handler import handle_voice_building_selection
        handle_voice_building_selection(sender, building, user)
    elif state.startswith("create_") or next_action == "create_task":
        from facilities.flows.create_task import handle_building_selection
        handle_building_selection(sender, building, user)
    elif state == "overdue_tasks_building" or next_action == "overdue_tasks":
        from facilities.flows.overdue_tasks import show_overdue_tasks
        show_overdue_tasks(sender, user, building)
    elif state == "completed_tasks_building" or next_action == "completed_tasks":
        from facilities.flows.completed_tasks import show_completed_tasks
        show_completed_tasks(sender, user, building)
    elif state == "building_filter" or next_action == "team_tasks":
        from facilities.flows.team_tasks import show_team_tasks
        show_team_tasks(sender, building, user)
    else:
        from facilities.flows.team_tasks import show_team_tasks
        show_team_tasks(sender, building, user)


def _show_help(sender: str):
    """Send help message."""
    from whatsapp.ux import send_text
    msg = (
        "🔧 *GEI Facilities Bot — Help*\n\n"
        "Here's what I can do:\n\n"
        "📋 *My Tasks* — View your assigned tasks\n"
        "👥 *Team Tasks* — View tasks by building\n"
        "➕ *Create Task* — Create a new facilities task\n"
        "🔄 *Update Task* — Update status or add notes\n"
        "📊 *Summary* — Task counts by building/status\n"
        "🔗 *Sync Status* — Google Sheets sync health\n"
        "📜 *History* — View full audit trail for a task\n"
        "📝 *Daily Digest* — Today's activity summary\n\n"
        "_You can also just tell me what you need in plain language, "
        "or send a voice note!_\n\n"
        "Type *menu* to see the main menu."
    )
    send_text(sender, msg)


# ── Entry Point Check ────────────────────────────────────────────────────────

def is_facilities_user(whatsapp_number: str) -> bool:
    """
    Quick check if a WhatsApp number belongs to a Facilities user.
    Used by the main webhook to decide whether to route to this module.
    """
    clean_num = clean_phone_number(whatsapp_number)
    if clean_num in KANAV_PHONES or clean_num in (KANAV_PHONE, DEVELOPER_PHONE):
        return True
    user = resolve_facilities_user(clean_num or whatsapp_number)
    return user is not None and user.get("is_facilities_user", False)
