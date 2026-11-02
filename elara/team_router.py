from __future__ import annotations
import time
import logging
import re
import threading
from whatsapp.ux import clean_phone_number, send_interactive_buttons, send_text
from elara.auth import resolve_elara_user, is_elara_user, is_elara_admin
from elara.config import ELARA_DEPARTMENTS, DEVELOPER_PHONE, KANAV_PHONE, KANAV_PHONES
from facilities.auth import resolve_facilities_user
from facilities.config import BUILDING_TABS, BUILDING_ALIASES

logger = logging.getLogger(__name__)

# In-memory store for active locked team context and pending requests
_team_lock = threading.Lock()
_active_team_contexts: dict[str, dict] = {}   # sender -> {"team": "elara" | "facilities", "expires_at": float}
_pending_actions: dict[str, dict] = {}        # sender -> {"text": str, "timestamp": float}

CONTEXT_TTL_SECONDS = 900  # 15 minutes


def set_active_team(sender: str, team: str):
    """Lock team context ('elara' or 'facilities') for this sender."""
    clean_num = clean_phone_number(sender)
    with _team_lock:
        _active_team_contexts[clean_num] = {
            "team": team.lower(),
            "expires_at": time.time() + CONTEXT_TTL_SECONDS,
        }


def get_active_team(sender: str) -> str | None:
    """Get currently locked team context, if any."""
    clean_num = clean_phone_number(sender)
    with _team_lock:
        ctx = _active_team_contexts.get(clean_num)
        if not ctx:
            return None
        if time.time() > ctx.get("expires_at", 0):
            _active_team_contexts.pop(clean_num, None)
            return None
        return ctx.get("team")


def clear_active_team(sender: str):
    """Clear active team context and pending action for a sender."""
    clean_num = clean_phone_number(sender)
    with _team_lock:
        _active_team_contexts.pop(clean_num, None)
        _pending_actions.pop(clean_num, None)


def clear_all_team_contexts():
    """Clear all active team contexts and pending actions across all users."""
    with _team_lock:
        _active_team_contexts.clear()
        _pending_actions.clear()



def set_pending_action(sender: str, text: str):
    """Store ambiguous request while waiting for Kanav's team choice."""
    clean_num = clean_phone_number(sender)
    with _team_lock:
        _pending_actions[clean_num] = {
            "text": text,
            "timestamp": time.time(),
        }


def pop_pending_action(sender: str) -> str | None:
    """Retrieve and clear pending request for sender."""
    clean_num = clean_phone_number(sender)
    with _team_lock:
        item = _pending_actions.pop(clean_num, None)
        if item and time.time() - item.get("timestamp", 0) < 600:
            return item.get("text")
        return None


def is_dual_access_user(whatsapp_number: str) -> bool:
    """Check if user has access to BOTH Facilities and Elara Home (Kanav / Developer)."""
    clean_num = clean_phone_number(whatsapp_number)
    if clean_num in KANAV_PHONES or clean_num in (KANAV_PHONE, DEVELOPER_PHONE):
        return True

    # Check if registered in both systems
    elara_u = resolve_elara_user(whatsapp_number)
    fac_u = resolve_facilities_user(whatsapp_number)
    if elara_u and fac_u:
        return True

    return False


# Known team members for team disambiguation
KNOWN_ELARA_MEMBERS = ("rizwan", "rachit", "bhagwan dass", "bhagwan", "gaurav")
KNOWN_FACILITIES_MEMBERS = ("anoop", "kuldeep", "vikash", "vikram", "vikramjeet", "gopal", "ritesh", "arjun", "chandan")


def detect_team_intent_from_text(text: str) -> str | None:
    """
    Detect whether text explicitly mentions Elara Home vs Facilities.
    Returns: 'elara', 'facilities', or None (ambiguous).
    """
    clean = text.strip().lower()

    # 1. Explicit Elara Home markers
    if "elara" in clean:
        return "elara"

    # Check for the 6 canonical Elara departments
    for dept in ELARA_DEPARTMENTS:
        if dept.lower() in clean:
            return "elara"

    # 2. Explicit Facilities markers
    if "facilities" in clean or "facility" in clean:
        return "facilities"

    # Check for Facilities buildings or aliases
    for bldg in BUILDING_TABS:
        if bldg.lower() in clean:
            return "facilities"

    for alias in BUILDING_ALIASES:
        # Match whole words only for short aliases like "bay 1", "gett"
        if re.search(rf'\b{re.escape(alias)}\b', clean):
            return "facilities"

    # Facilities ref no pattern (e.g. GEBB1-001, GETT-123)
    if re.search(r'\b(gebb1|gebb2|gett|com)-\d{1,4}\b', clean, re.I):
        return "facilities"

    # 3. Known team member matching (word boundaries)
    # Check Elara team members (dynamic + static fallback)
    elara_names = set(KNOWN_ELARA_MEMBERS)
    try:
        from elara.auth import get_elara_team_members
        for m in get_elara_team_members():
            m_name = (m.get("name") or "").strip().lower()
            if m_name and m_name not in ("kanav", "developer"):
                elara_names.add(m_name)
    except Exception:
        pass

    for name in elara_names:
        if re.search(rf'\b{re.escape(name)}\b', clean):
            return "elara"

    # Check Facilities team members (word boundaries)
    for name in KNOWN_FACILITIES_MEMBERS:
        if re.search(rf'\b{re.escape(name)}\b', clean):
            return "facilities"

    return None


def send_team_selection_prompt(to: str, custom_body: str | None = None):
    """
    Prompt Kanav / Developer with team selection buttons:
    "For which Team do you want to do this?"
    [Facilities Team]
    [Elara Home]
    [Factech Automation] (for developer)
    """
    body = custom_body or "For which Team do you want to do this?"
    clean_num = clean_phone_number(to)
    buttons = [
        {"id": "team_sel_facilities", "title": "Facilities Team"},
        {"id": "team_sel_elara", "title": "Elara Home"},
    ]
    if clean_num == DEVELOPER_PHONE:
        buttons.append({"id": "team_sel_factech", "title": "Factech Automation"})

    ok = send_interactive_buttons(to, body, buttons)
    if not ok:
        menu_items = (
            f"{body}\n\n"
            f"1️⃣ *Facilities Team*\n"
            f"2️⃣ *Elara Home*\n"
        )
        if clean_num == DEVELOPER_PHONE:
            menu_items += f"3️⃣ *Factech Automation*\n\n_Reply with 1, 2, or 3, or type 'facilities', 'elara', or 'factech'._"
        else:
            menu_items += f"\n_Reply with 1 or 2, or type 'facilities' or 'elara'._"
        send_text(to, menu_items)
    return ok


def route_incoming_message(sender: str, text: str | None = None, button_id: str | None = None,
                           voice_transcript: str | None = None) -> bool:
    """
    Master router for all incoming WhatsApp messages.
    Returns True if the message was handled.
    """
    clean_num = clean_phone_number(sender)

    # ── Chaitanya Temporary Tenant Override ──────────────────────────────────
    from clients.config import TREAT_CHAITANYA_AS_TENANT_ONLY, is_chaitanya
    if TREAT_CHAITANYA_AS_TENANT_ONLY and is_chaitanya(clean_num):
        return False

    msg_text = voice_transcript if voice_transcript else (text or "")
    clean_msg = msg_text.strip().lower()


    elara_user = resolve_elara_user(clean_num)
    fac_user = resolve_facilities_user(clean_num)

    # ── 1. Handle Team Selection Buttons ─────────────────────────────────────
    if button_id in ("team_sel_facilities", "team_sel_elara", "team_sel_factech"):
        if button_id == "team_sel_factech":
            selected_team = "factech"
        elif button_id == "team_sel_facilities":
            selected_team = "facilities"
        else:
            selected_team = "elara"

        set_active_team(clean_num, selected_team)

        from elara.session import clear_elara_session
        from facilities.flows.router import clear_session as clear_fac_session
        clear_elara_session(clean_num)
        clear_fac_session(clean_num)

        pending_text = pop_pending_action(clean_num)
        if selected_team == "factech":
            from clients.flows import handle_client_hi
            send_text(sender, "🏢 Switched context to *Factech Automation (Tenant Mode)*.\n\nYou are now in the tenant testing flow.")
            handle_client_hi(sender)
            return True
        elif selected_team == "elara":
            from elara.flows.router import route_elara_message
            from elara.flows.home import show_elara_home
            send_text(sender, "🏠 Switched context to *Elara Home*.")
            if pending_text:
                route_elara_message(sender, text=pending_text, user=elara_user)
            else:
                show_elara_home(sender, elara_user or {"name": "Kanav", "role": "Director"})
            return True
        else:
            from facilities.flows.router import route_facilities_message
            from facilities.flows.home import show_home
            send_text(sender, "🏢 Switched context to *Facilities Team*.")
            if pending_text:
                route_facilities_message(sender, text=pending_text, user=fac_user or {})
            else:
                show_home(sender, fac_user or {"name": "Kanav", "role": "Director"})
            return True

    if button_id == "team_sel_prompt":
        send_team_selection_prompt(sender, "Which Team would you like to access?")
        return True

    # ── 2. Handle Elara-Specific Interactive Buttons ─────────────────────────
    if button_id and button_id.startswith("elara_"):
        set_active_team(clean_num, "elara")
        from elara.flows.router import route_elara_message
        route_elara_message(sender, button_id=button_id, user=elara_user)
        return True

    # ── 3. Handle Facilities-Specific Interactive Buttons ───────────────────
    if button_id and button_id.startswith("fac_"):
        set_active_team(clean_num, "facilities")
        from facilities.flows.router import route_facilities_message
        route_facilities_message(sender, button_id=button_id, user=fac_user or {})
        return True

    # If an unknown/core interactive button was received, do not intercept it
    if button_id:
        return False

    # ── 4. Categorize User Membership ────────────────────────────────────────

    # Priority 1: DUAL-ACCESS USER (Kanav Director, Developer)
    if is_dual_access_user(clean_num):
        active_team = get_active_team(clean_num)

        # Developer Factech mode active context handling
        if clean_num == DEVELOPER_PHONE and active_team == "factech":
            if clean_msg in ("switch team", "switch teams", "change team", "team menu", "reset", "clear", "cancel"):
                clear_active_team(clean_num)
                body = "👋 Hello *Developer*!\n\nWhich Team would you like to access?"
                send_team_selection_prompt(sender, custom_body=body)
                return True
            elif clean_msg in ("switch to facilities", "facilities team", "facilities"):
                set_active_team(clean_num, "facilities")
                from facilities.flows.home import show_home
                send_text(sender, "🏢 Switched context to *Facilities Team*.")
                show_home(sender, fac_user or {"name": "Developer", "role": "Developer"})
                return True
            elif clean_msg in ("switch to elara", "elara home", "open elara", "elara"):
                set_active_team(clean_num, "elara")
                from elara.flows.home import show_elara_home
                send_text(sender, "🏠 Switched context to *Elara Home*.")
                show_elara_home(sender, elara_user or {"name": "Developer", "role": "Developer"})
                return True
            elif clean_msg in ("switch to factech", "switch factech", "factech", "factech automation", "open factech"):
                set_active_team(clean_num, "factech")
                send_text(sender, "🏢 Switched context to *Factech Automation (Tenant Mode)*.\n\nYou are now in the tenant testing flow.")
                from clients.flows import handle_client_hi
                handle_client_hi(sender)
                return True
            # When testing Factech, allow all other messages (greetings, 1/2/3, complaints) to flow to Factech
            return False

        # 1. Greeting & Reset check ("hi", "hello", "hey", "start", "menu", "reset", "clear", "cancel"):
        # Explicit requirement: Clears all cached sessions & active team context
        # and prompts for team selection so user always gets a clean, fresh start.
        greeting_words = ("hi", "hello", "hey", "start", "menu", "reset", "clear", "cancel")
        if clean_msg in greeting_words:
            from elara.session import clear_elara_session
            from facilities.flows.router import clear_session as clear_fac_session
            from db import supabase
            clear_elara_session(clean_num)
            clear_fac_session(clean_num)
            with _team_lock:
                _active_team_contexts.pop(clean_num, None)
                _pending_actions.pop(clean_num, None)
            try:
                supabase.table("wa_task_states").delete().eq("whatsapp_number", clean_num).execute()
            except Exception:
                pass

            user_name = (elara_user or fac_user or {}).get("name", "Kanav")
            body = (
                f"👋 Hello *{user_name}*!\n\n"
                f"Which Team would you like to access today?"
            )
            send_team_selection_prompt(sender, custom_body=body)
            return True

        # 2. Explicit switch command check
        if clean_msg in ("switch to factech", "switch factech", "factech", "factech automation", "open factech"):
            set_active_team(clean_num, "factech")
            from elara.session import clear_elara_session
            from facilities.flows.router import clear_session as clear_fac_session
            clear_elara_session(clean_num)
            clear_fac_session(clean_num)
            send_text(sender, "🏢 Switched context to *Factech Automation (Tenant Mode)*.\n\nYou are now in the tenant testing flow.")
            from clients.flows import handle_client_hi
            handle_client_hi(sender)
            return True

        if clean_msg == "3" and clean_num == DEVELOPER_PHONE:
            set_active_team(clean_num, "factech")
            from elara.session import clear_elara_session
            from facilities.flows.router import clear_session as clear_fac_session
            clear_elara_session(clean_num)
            clear_fac_session(clean_num)
            send_text(sender, "🏢 Switched context to *Factech Automation (Tenant Mode)*.\n\nYou are now in the tenant testing flow.")
            from clients.flows import handle_client_hi
            handle_client_hi(sender)
            return True

        if clean_msg in ("switch team", "switch to elara", "elara home", "open elara"):
            set_active_team(clean_num, "elara")
            from elara.flows.home import show_elara_home
            show_elara_home(sender, elara_user or {"name": "Kanav", "role": "Director"})
            return True

        if clean_msg in ("switch to facilities", "facilities team", "facilities"):
            set_active_team(clean_num, "facilities")
            from facilities.flows.home import show_home
            show_home(sender, fac_user or {"name": "Kanav", "role": "Director"})
            return True

        # 3. Check if text clearly indicates a specific team
        detected_team = detect_team_intent_from_text(msg_text)
        if detected_team == "elara":
            set_active_team(clean_num, "elara")
            from facilities.flows.router import clear_session as clear_fac_session
            clear_fac_session(clean_num)
            from elara.flows.router import route_elara_message
            route_elara_message(sender, text=text, button_id=button_id, user=elara_user, voice_transcript=voice_transcript)
            return True
        elif detected_team == "facilities":
            set_active_team(clean_num, "facilities")
            from elara.session import clear_elara_session
            clear_elara_session(clean_num)
            from facilities.flows.router import route_facilities_message
            route_facilities_message(
                sender,
                text=text,
                button_id=button_id,
                user=fac_user,
                voice_transcript=voice_transcript,
            )
            return True

        # 4. Check if user is in an active in-flight multi-step flow (when no explicit team mentioned)
        from elara.session import get_elara_session
        from facilities.flows.router import get_session as get_fac_session

        elara_sess = get_elara_session(clean_num)
        if elara_sess and isinstance(elara_sess, dict) and elara_sess.get("flow_state"):
            from elara.flows.router import route_elara_message
            route_elara_message(sender, text=text, button_id=button_id, user=elara_user, voice_transcript=voice_transcript)
            return True

        fac_sess = get_fac_session(clean_num)
        if fac_sess and isinstance(fac_sess, dict) and fac_sess.get("current_flow_state"):
            from facilities.flows.router import route_facilities_message
            route_facilities_message(sender, text=text or "", button_id=button_id or "", user=fac_user or {}, voice_transcript=voice_transcript or "")
            return True

        # 5. Check if active locked team context exists from recent operation
        active_team = get_active_team(clean_num)
        if active_team == "elara":
            from elara.flows.router import route_elara_message
            route_elara_message(sender, text=text, button_id=button_id, user=elara_user, voice_transcript=voice_transcript)
            return True
        elif active_team == "facilities":
            from facilities.flows.router import route_facilities_message
            route_facilities_message(
                sender,
                text=text,
                button_id=button_id,
                user=fac_user,
                voice_transcript=voice_transcript,
            )
            return True
        elif active_team == "factech":
            return False

        # 6. Team intent is UNCLEAR / AMBIGUOUS for Kanav/Developer:
        # Prompt Kanav: "For which Team do you want to do this?" with buttons [Facilities Team] and [Elara Home]
        logger.info(f"Ambiguous team intent from dual-access user {sender}: '{msg_text}'. Prompting team selection.")
        set_pending_action(clean_num, msg_text)
        send_team_selection_prompt(sender)
        return True

    # Priority 2: ELARA-ONLY USER (e.g. Rachit, Bhagwan Dass, Gaurav)
    if elara_user and (not fac_user or not fac_user.get("is_facilities_user")):
        from elara.flows.router import route_elara_message
        route_elara_message(sender, text=text, button_id=button_id, user=elara_user, voice_transcript=voice_transcript)
        return True

    # Priority 3: FACILITIES-ONLY USER (e.g. Anoop, Kuldeep, Vikash)
    if fac_user and fac_user.get("is_facilities_user") and not elara_user:
        from facilities.flows.router import route_facilities_message
        route_facilities_message(
            sender,
            text=text,
            button_id=button_id,
            user=fac_user,
            voice_transcript=voice_transcript,
        )
        return True

    # User is not a recognized team member of either team (guest)
    return False
