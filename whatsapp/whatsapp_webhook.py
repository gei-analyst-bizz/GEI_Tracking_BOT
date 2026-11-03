from __future__ import annotations
"""
WhatsApp Webhook — Production Entry Point
==========================================
Handles:
  - GET  /webhook  → Meta verification handshake
  - POST /webhook  → Incoming messages (text + interactive button replies + voice notes)

Flow routing:
  1. Interactive button replies → task_assignment.handle_button_reply()
  2. Audio messages (voice notes) → audio_handler.handle_voice_note() → core engine
  3. Text messages in WA state  → task_assignment.handle_text_reply()
  4. All other text             → core bot engine (process_user_message)
"""

from dotenv import load_dotenv
load_dotenv()

import os
import sys
import logging
import asyncio
import requests
import threading

# Ensure project root is in sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# pyrefly: ignore [missing-import]
from flask import Flask, request, jsonify
from core.logic import process_user_message
from core.error_messages import friendly_system_error
from whatsapp.task_assignment import (
    send_text,
    handle_button_reply,
    handle_text_reply,
    _send_document_wa,
)

app = Flask(__name__)

# ── Register Feedback API Blueprint ──────────────────────────────────────────
from feedback.routes import feedback_bp
app.register_blueprint(feedback_bp)

# ── Register Teams API Blueprint ───────────────────────────────────────────
from teams.api import teams_bp
app.register_blueprint(teams_bp)

# ── Register Tasks API Blueprint ───────────────────────────────────────────
from tasks.api import tasks_bp
app.register_blueprint(tasks_bp)

# ── Register Reports API Blueprint ─────────────────────────────────────────
from reports.api import reports_bp
app.register_blueprint(reports_bp)

# ── Session restore + scheduler start are deferred — see _deferred_startup() below ─


# ── Background Scheduler (shared for all cron jobs) ─────────────────────────
import pytz
from config import TIMEZONE
# pyrefly: ignore [missing-import]
from apscheduler.schedulers.background import BackgroundScheduler

try:
    _wa_tz = pytz.timezone(TIMEZONE or "Asia/Kolkata")
except Exception:
    _wa_tz = pytz.timezone("Asia/Kolkata")

_bg_scheduler = BackgroundScheduler(timezone=_wa_tz)


def whatsapp_daily_report_job():
    """Sends the daily PDF report and daily updates summary to Kanav on WhatsApp at 6 PM."""
    logging.info("Running WhatsApp daily report job (6 PM)...")
    try:
        from db import supabase
        from core.intent_handlers import generate_pdf_report
        import datetime
        
        # 1. Resolve Kanav's WhatsApp number
        res = supabase.table("users").select("whatsapp_number").eq("name", "Kanav").execute()
        if not res.data or not res.data[0].get("whatsapp_number"):
            logging.error("WhatsApp daily report: Kanav has no whatsapp_number configured")
            return
        
        kanav_wa = res.data[0]["whatsapp_number"]
        
        # 2. Generate Facilities EOD PDF and send to Kanav (Disabled for now)
        from facilities.config import ENABLE_FACILITIES_EOD_REPORT
        if ENABLE_FACILITIES_EOD_REPORT:
            try:
                from facilities.eod_report import send_facilities_eod_report
                send_facilities_eod_report(kanav_wa, send_summary_text=False)
                logging.info(f"WhatsApp Facilities EOD PDF report (Facilities_Report_EOD.pdf) sent to Kanav ({kanav_wa})")
            except Exception as fac_pdf_err:
                logging.error(f"Error generating/sending Facilities EOD PDF report on WhatsApp: {fac_pdf_err}")
                # Fallback to general project report if facilities report generation encountered an error
                try:
                    pdf_path = generate_pdf_report()
                    _send_document_wa(kanav_wa, pdf_path)
                except Exception as pdf_err:
                    logger.error(f"Fallback PDF generation error: {pdf_err}")
        else:
            logging.info("Evening Facilities EOD report is disabled for now. Skipping Facilities EOD PDF delivery.")
            
        # 3. Generate multiline updates text summary and send
        try:
            today = datetime.date.today().isoformat()
            upds = supabase.table("daily_updates").select("*, projects(name), tasks(title), users(name)").gte("timestamp", today).execute()
            if upds.data:
                from collections import defaultdict
                grouped = defaultdict(list)
                for u in upds.data: 
                    grouped[u.get("projects", {}).get("name", "Unknown")].append(u)
                
                report = "📝 *Daily Updates Summary*\n\n"
                for p, lu in grouped.items():
                    report += f"*{p}*\n"
                    for u in lu:
                        report += f"- {u.get('tasks', {}).get('name', 'Task')} ({u['progress']}%) by {u.get('users', {}).get('name', 'User')}\n"
                        if u.get('blocker') and u['blocker'].lower() not in ["no blocker", "none"]: 
                            report += f"  🛑 Blocker: {u['blocker']}\n"
                    report += "\n"
                
                send_text(kanav_wa, report)
                logging.info(f"WhatsApp daily update summary sent to Kanav ({kanav_wa})")
        except Exception as txt_err:
            logging.error(f"Error generating/sending daily text report on WhatsApp: {txt_err}")
            
    except Exception as e:
        logging.error(f"Error in whatsapp_daily_report_job: {e}", exc_info=True)


def whatsapp_employee_reminder_job():
    """Sends a reminder to employees (e.g. Asif) at 5 PM if they haven't sent any update today."""
    logging.info("Running WhatsApp employee reminder job (5 PM)...")
    try:
        from db import supabase, get_active_users_with_tasks, get_tasks_for_user
        import datetime
        
        # 1. Get all active users with pending tasks
        users = get_active_users_with_tasks()
        if not users:
            logging.info("No active users with tasks for reminder")
            return
            
        today = datetime.date.today().isoformat()
        
        for user in users:
            wa_num = user.get("whatsapp_number")
            if not wa_num:
                continue
                
            # 2. Check if user has updated any tasks today (updates or daily_updates)
            # Check updates table
            upds_res = supabase.table("updates").select("id").eq("employee_id", user["id"]).gte("timestamp", today).execute()
            # Check daily_updates table
            dupds_res = supabase.table("daily_updates").select("id").eq("user_id", user["id"]).gte("timestamp", today).execute()
            
            if not upds_res.data and not dupds_res.data:
                # No updates today! Send reminder.
                tasks = get_tasks_for_user(user["id"])
                active_tasks = [t for t in tasks if t["status"] != "Completed"]
                if not active_tasks:
                    continue
                    
                msg = f"Hi {user['name']} 👋\n"
                msg += "You haven't updated any tasks today. Please share updates on your ongoing tasks:\n\n"
                for i, t in enumerate(active_tasks, 1):
                    p_name = t["projects"]["name"] if t.get("projects") else "No Project"
                    msg += f"{i}. {t['name']} - {p_name}\n"
                msg += "\nReply with updates in natural language or record a voice note."
                
                send_text(wa_num, msg)
                logging.info(f"WhatsApp employee reminder sent to {user['name']} ({wa_num})")
    except Exception as e:
        logging.error(f"Error in whatsapp_employee_reminder_job: {e}", exc_info=True)


# ── Feedback reminder cron ───────────────────────────────────────────────────
try:
    from feedback.reminders import check_and_send_reminders
    from feedback.config import REMINDER_CRON_INTERVAL
    _bg_scheduler.add_job(
        check_and_send_reminders,
        'interval',
        seconds=REMINDER_CRON_INTERVAL,
        id='feedback_reminders',
        replace_existing=True,
        max_instances=2,     # Let APScheduler allow overlap — our own lock handles it
        coalesce=True,       # Collapse missed runs into one
    )
    logging.info(f"Feedback reminder cron registered (every {REMINDER_CRON_INTERVAL}s)")
except Exception as _cron_err:
    logging.warning(f"Feedback reminder cron failed to register: {_cron_err}")

# ── WhatsApp Daily Report Job (6 PM Mon-Sat) ──────────────────────────
try:
    _bg_scheduler.add_job(
        whatsapp_daily_report_job,
        'cron',
        day_of_week='mon-sat',
        hour=18,
        minute=0,
        id='whatsapp_daily_report',
        replace_existing=True,
    )
    logging.info("WhatsApp daily report job scheduled at 6:00 PM (Mon-Sat)")
except Exception as _report_sched_err:
    logging.warning(f"WhatsApp daily report job failed to schedule: {_report_sched_err}")

# ── WhatsApp Employee Inactivity Reminder (5 PM Mon-Sat) ─────────────
try:
    _bg_scheduler.add_job(
        whatsapp_employee_reminder_job,
        'cron',
        day_of_week='mon-sat',
        hour=17,
        minute=0,
        id='whatsapp_employee_reminder',
        replace_existing=True,
    )
    logging.info("WhatsApp employee reminder job scheduled at 5:00 PM (Mon-Sat)")
except Exception as _rem_sched_err:
    logging.warning(f"WhatsApp employee reminder job failed to schedule: {_rem_sched_err}")

# ── Keep-Alive Self-Ping (prevents Render free-tier sleep) ───────────────────
_RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://gei-whatsapp-tracking-bot.onrender.com")
_KEEP_ALIVE_INTERVAL = int(os.getenv("KEEP_ALIVE_INTERVAL", "600"))  # 10 min default


def _keep_alive_ping():
    """Self-ping /health to keep Render from spinning down."""
    try:
        url = f"{_RENDER_URL}/health"
        resp = requests.get(url, timeout=15)
        logging.info(f"Keep-alive ping → {resp.status_code}")
    except Exception as e:
        logging.warning(f"Keep-alive ping failed: {e}")


_bg_scheduler.add_job(
    _keep_alive_ping,
    'interval',
    seconds=_KEEP_ALIVE_INTERVAL,
    id='keep_alive_ping',
    replace_existing=True,
)
logging.info(f"Keep-alive self-ping registered (every {_KEEP_ALIVE_INTERVAL}s → {_RENDER_URL}/health)")

# ── Facilities Google Sheets Polling & Retry Jobs ───────────────────────────
try:
    from facilities.sync_engine import poll_sheet_changes, retry_failed_syncs
    from facilities.config import FACILITIES_POLL_INTERVAL
    _bg_scheduler.add_job(
        poll_sheet_changes,
        'interval',
        seconds=FACILITIES_POLL_INTERVAL,
        id='facilities_poll_sheet',
        replace_existing=True,
    )
    _bg_scheduler.add_job(
        retry_failed_syncs,
        'interval',
        seconds=FACILITIES_POLL_INTERVAL,
        id='facilities_retry_syncs',
        replace_existing=True,
    )
    logging.info(f"Facilities Sheet polling & retry jobs registered (every {FACILITIES_POLL_INTERVAL}s)")
except Exception as _fac_sched_err:
    logging.warning(f"Facilities background jobs failed to schedule: {_fac_sched_err}")

# ── Compliance Google Sheet Daily Reminder Job ─────────────────────────────
try:
    from compliance.scheduler_job import run_compliance_daily_check
    from compliance.config import COMPLIANCE_CHECK_HOUR, COMPLIANCE_CHECK_MINUTE
    _bg_scheduler.add_job(
        run_compliance_daily_check,
        'cron',
        hour=COMPLIANCE_CHECK_HOUR,
        minute=COMPLIANCE_CHECK_MINUTE,
        id='compliance_daily_reminder',
        replace_existing=True,
    )
    logging.info(f"Compliance daily reminder job registered (at {COMPLIANCE_CHECK_HOUR:02d}:{COMPLIANCE_CHECK_MINUTE:02d} daily)")
except Exception as _comp_sched_err:
    logging.warning(f"Compliance daily reminder job failed to schedule: {_comp_sched_err}")

# ── Client Master Data Periodic Sync Job ──────────────────────────────────
try:
    from clients.sync_engine import run_client_master_sync
    from clients.config import CLIENT_SYNC_INTERVAL_MINUTES
    _bg_scheduler.add_job(
        run_client_master_sync,
        'interval',
        minutes=CLIENT_SYNC_INTERVAL_MINUTES,
        id='client_master_periodic_sync',
        replace_existing=True,
    )
    logging.info(f"Client master periodic sync registered (every {CLIENT_SYNC_INTERVAL_MINUTES}m)")
except Exception as _client_sched_err:
    logging.warning(f"Client master periodic sync failed to schedule: {_client_sched_err}")



# ── Deferred Startup ────────────────────────────────────────────────────────
# Start scheduler + restore sessions AFTER gunicorn binds the port.
# This ensures Render's health check can respond immediately on deploy.
def _deferred_startup():
    import time as _time
    _time.sleep(10)  # give gunicorn time to bind the port

    # 1. Start the background scheduler
    try:
        _bg_scheduler.start()
        logging.info("Background scheduler started (deferred)")
    except Exception as _sched_err:
        logging.error(f"Failed to start scheduler: {_sched_err}", exc_info=True)

    # 2. Restore feedback sessions from Google Sheet
    try:
        from feedback.session_store import restore_sessions_from_sheet
        restore_sessions_from_sheet()
        logging.info("Feedback sessions restored from sheet.")
    except Exception as _fb_err:
        logging.warning(f"Feedback session restore skipped: {_fb_err}")

    # 3. Initial Client Master Sync
    try:
        from clients.sync_engine import run_client_master_sync
        run_client_master_sync()
        logging.info("Initial Client Master Sync completed.")
    except Exception as _sync_err:
        logging.warning(f"Initial Client Master Sync skipped: {_sync_err}")



if os.environ.get("TESTING") != "1":
    threading.Thread(target=_deferred_startup, daemon=True, name="deferred-startup").start()
    logging.info("Deferred startup scheduled (scheduler + session restore in ~10s)")
else:
    logging.info("Deferred startup skipped (TESTING mode active)")


# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Env vars ─────────────────────────────────────────────────────────────────
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")


# ─────────────────────────────────────────────────────────────────────────────
# WEBHOOK VERIFICATION (GET)
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint — used by keep-alive ping and Render health checks."""
    try:
        from clients import sync_engine
        diag = sync_engine.get_sync_diagnostics()
        return jsonify({
            "status": "ok",
            "service": "gei-whatsapp-bot",
            "uptime": "alive",
            "clients_count": diag.get("cached_clients_count", 0),
            "safwan_recognized": diag.get("safwan_recognized", False),
            "active_sa": diag.get("active_service_account"),
            "last_sync_iso": diag.get("last_sync_iso"),
        }), 200
    except Exception as e:
        return jsonify({
            "status": "ok",
            "service": "gei-whatsapp-bot",
            "uptime": "alive",
            "diag_error": str(e),
        }), 200


@app.route('/api/clients/status', methods=['GET'])
def api_clients_status():
    """Public / admin status endpoint for Google Sheets client sync."""
    try:
        from clients import sync_engine
        return jsonify({
            "status": "ok",
            "diagnostics": sync_engine.get_sync_diagnostics(),
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route('/api/clients/sync', methods=['POST', 'GET'])
def api_clients_sync():
    """Forces an immediate client sync from the Google Sheet."""
    try:
        from clients import sync_engine
        include_all = request.args.get("all_tabs", "false").lower() in ("true", "1", "yes")
        stats = sync_engine.force_client_master_sync(include_all_tabs=include_all)
        return jsonify({
            "status": "ok",
            "sync_stats": stats,
            "diagnostics": sync_engine.get_sync_diagnostics(),
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route('/webhook', methods=['GET'])
def verify_webhook():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("Webhook verified successfully")
        # pyrefly: ignore [unnecessary-type-conversion]
        return str(challenge or ""), 200
    return "Verification failed", 403


# ─────────────────────────────────────────────────────────────────────────────
# INCOMING MESSAGES (POST)
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/webhook', methods=['POST'])
def handle_whatsapp_message():
    try:
        body = request.get_json()
        if not body:
            return jsonify({"status": "no data"}), 200

        # print(flush=True) guarantees visibility on Render — logger.info is often invisible
        print(f"[WEBHOOK] POST /webhook received", flush=True)
        logger.info(f"Incoming WA payload: {str(body)[:500]}")

        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                # ── Skip delivery/read status updates ────────────────────────
                if value.get("statuses"):
                    print(f"[WEBHOOK] Skipping status update (delivery/read receipt)", flush=True)
                    continue

                messages = value.get("messages", [])
                if not messages:
                    print(f"[WEBHOOK] No messages in this change entry", flush=True)
                    continue

                for message in messages:
                    sender = message.get("from", "")
                    msg_type = message.get("type", "")
                    print(f"[WEBHOOK] Message from={sender} type={msg_type}", flush=True)

                    # ── Dispatch to background thread to prevent Meta webhook timeout ─
                    if msg_type == "interactive":
                        i_type = message.get("interactive", {}).get("type", "unknown")
                        print(f"[WEBHOOK] Interactive sub-type={i_type} from {sender} — dispatching to background", flush=True)
                        threading.Thread(target=_handle_interactive, args=(sender, message), daemon=False).start()

                    # ── Template Quick Reply button ───────────────────────────
                    elif msg_type == "button":
                        print(f"[WEBHOOK] Template button reply from {sender} — dispatching to background", flush=True)
                        threading.Thread(target=_handle_template_button, args=(sender, message), daemon=False).start()

                    # ── Voice note (audio) ─────────────────────────────────────
                    elif msg_type == "audio":
                        threading.Thread(target=_handle_audio, args=(sender, message), daemon=True).start()

                    # ── Image ─────────────────────────────────────────────────
                    elif msg_type == "image":
                        threading.Thread(target=_handle_image, args=(sender, message), daemon=True).start()

                    # ── Document (PDF, etc.) ──────────────────────────────────
                    elif msg_type == "document":
                        threading.Thread(target=_handle_document, args=(sender, message), daemon=True).start()

                    # ── Plain text ────────────────────────────────────────────
                    elif msg_type == "text":
                        text = message.get("text", {}).get("body", "").strip()
                        if text:
                            def _process_text_bg(sender_num, msg_text):
                                # Check for clear/reset command first
                                if msg_text.strip().upper() in ("CLEAR", "CLEAR CHAT", "RESET", "CLEAR SESSION", "RESTART"):
                                    try:
                                        from feedback.session_store import force_clear_and_process_next
                                        from core.conversation_state import clear_state
                                        from core.context_manager import clear_context
                                        from elara.session import clear_elara_session
                                        
                                        force_clear_and_process_next(sender_num)
                                        clear_state(sender_num)
                                        clear_context(sender_num)
                                        clear_elara_session(sender_num)
                                        # pyrefly: ignore[unbound-name]
                                        send_text(sender_num, "Chat history and active feedback sessions have been cleared! 🧹")
                                        return
                                    except Exception as clear_err:
                                        logger.error(f"Error clearing WhatsApp state for {sender_num}: {clear_err}")

                                # ── Master Team Router (Elara Home / Facilities / Kanav team disambiguation) ──
                                try:
                                    from elara.team_router import route_incoming_message
                                    if route_incoming_message(sender_num, text=msg_text):
                                        return
                                except Exception as team_err:
                                    logger.error(f"Team router error for {sender_num}: {team_err}", exc_info=True)

                                from auth.middleware import authenticate_whatsapp_request
                                auth_user = authenticate_whatsapp_request(sender_num)
                                if not auth_user:
                                    logger.error(f"Auth failed for {sender_num}")
                                    return
                                
                                # Check for casual greetings (Main Menu / Client Flow trigger)
                                import re
                                clean_greeting = re.sub(r'[^\w\s]', '', msg_text).strip().lower()
                                greeting_words = ["hi", "hello", "menu", "hey", "start", "support", "help"]
                                is_greeting = any(
                                    clean_greeting == g or clean_greeting.startswith(f"{g} ")
                                    for g in greeting_words
                                )
                                from clients.flows import has_active_client_flow
                                if has_active_client_flow(sender_num):
                                    is_greeting = False

                                if is_greeting:
                                    # 1. Check if sender is a registered Factech / tenant client (or Chaitanya)
                                    is_client = False
                                    try:
                                        from clients.config import TREAT_CHAITANYA_AS_TENANT_ONLY, is_chaitanya
                                        from clients.flows import is_registered_client, handle_client_hi
                                        if (TREAT_CHAITANYA_AS_TENANT_ONLY and is_chaitanya(sender_num)) or is_registered_client(sender_num):
                                            is_client = True
                                            handle_client_hi(sender_num)
                                            return
                                    except Exception as client_hi_err:
                                        logger.error(f"Client greeting error for {sender_num}: {client_hi_err}", exc_info=True)
                                        if is_client:
                                            try:
                                                from whatsapp.task_assignment import send_text
                                                send_text(sender_num, "Hi 👋 Welcome to GEI Support. Please type 'menu' to view options or type your complaint description directly.")
                                            except Exception:
                                                pass
                                            return

                                    # 2. Check if sender is an internal employee / director / facilities / elara
                                    is_internal = (
                                        auth_user.get("role") in ("Director", "Employee", "Developer")
                                        or auth_user.get("department") == "Facilities"
                                    )
                                    from clients.config import TREAT_CHAITANYA_AS_TENANT_ONLY, is_chaitanya
                                    if TREAT_CHAITANYA_AS_TENANT_ONLY and is_chaitanya(sender_num):
                                        is_internal = False

                                    if not is_internal:
                                        try:
                                            from elara.auth import is_elara_user
                                            if is_elara_user(sender_num):
                                                is_internal = True
                                        except Exception:
                                            pass

                                    if not is_internal:
                                        from clients.flows import send_unregistered_client_message
                                        send_unregistered_client_message(sender_num)
                                        return


                                    try:
                                        from whatsapp.task_assignment import check_and_deliver_pending_task_notifications
                                        check_and_deliver_pending_task_notifications(auth_user)
                                    except Exception as check_err:
                                        logger.error(f"Error delivering pending task notifications: {check_err}")
                                    from whatsapp.menus import send_main_menu
                                    send_main_menu(sender_num, auth_user)
                                    return

                                # Feedback-first routing
                                if _try_feedback_route(sender_num, msg_text):
                                    return
                                _handle_text(sender_num, msg_text)
                            
                            threading.Thread(target=_process_text_bg, args=(sender, text), daemon=True).start()

                    else:
                        logger.info(f"Unsupported message type '{msg_type}' from {sender}")

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        print(f"[WEBHOOK] EXCEPTION: {e}", flush=True)
        return jsonify({"status": "error"}), 200


# ─────────────────────────────────────────────────────────────────────────────
# FEEDBACK ROUTING (intercepts before main bot engine)
# ─────────────────────────────────────────────────────────────────────────────

def _try_feedback_route(sender: str, text: str) -> bool:
    """
    Check if this sender has an active feedback session.
    If yes, route the message to the feedback engine and return True.
    If no, return False (fall through to main bot engine).
    """
    try:
        from feedback.engine import handle_feedback_reply
        return handle_feedback_reply(sender, text)
    except ImportError:
        logger.warning("Feedback module not available — skipping feedback route")
        return False
    except Exception as e:
        logger.error(f"Feedback routing error for {sender}: {e}", exc_info=True)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL ROUTING HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _handle_interactive(sender: str, message: dict):
    """
    Route interactive messages:
      - button_reply → task assignment module
      - nfm_reply    → WhatsApp Flow feedback response
    """
    try:
        interactive = message.get("interactive", {})
        i_type = interactive.get("type")
        print(f"[INTERACTIVE] Processing type={i_type} from {sender}", flush=True)

        if i_type in ("button_reply", "list_reply"):
            button_id = interactive[i_type]["id"]
            try:
                from elara.team_router import route_incoming_message
                if route_incoming_message(sender, button_id=button_id):
                    return
            except Exception as tr_err:
                logger.error(f"Team router interactive error: {tr_err}", exc_info=True)

            from auth.middleware import authenticate_whatsapp_request
            auth_user = authenticate_whatsapp_request(sender)
            if not auth_user:
                logger.error(f"Auth failed for {sender}")
                return

            from whatsapp.handlers import handle_interactive_reply
            handle_interactive_reply(sender, button_id, auth_user)

        elif i_type == "nfm_reply":
            # ── WhatsApp Flow form submission ─────────────────────────
            print(f"[INTERACTIVE] nfm_reply detected from {sender} — routing to feedback engine", flush=True)
            _handle_flow_response(sender, interactive)

        else:
            logger.warning(f"Unhandled interactive type '{i_type}' from {sender}")
            print(f"[INTERACTIVE] UNKNOWN type '{i_type}' from {sender}", flush=True)

    except Exception as e:
        # CRITICAL FIX: Was only catching (KeyError, TypeError) — any other
        # exception silently killed the thread with zero log output.
        logger.error(f"Error in _handle_interactive from {sender}: {e}", exc_info=True)
        print(f"[INTERACTIVE] EXCEPTION from {sender}: {type(e).__name__}: {e}", flush=True)


def _handle_template_button(sender: str, message: dict):
    """
    Route template quick reply button messages (msg_type == "button").
    Meta sends message.button.payload or message.button.text.
    """
    try:
        button = message.get("button", {})
        payload = button.get("payload") or button.get("text", "")
        print(f"[TEMPLATE_BUTTON] Processing button payload='{payload}' from {sender}", flush=True)

        try:
            from elara.team_router import route_incoming_message
            if route_incoming_message(sender, button_id=payload):
                return
        except Exception as tr_err:
            logger.error(f"Team router template error: {tr_err}", exc_info=True)

        from auth.middleware import authenticate_whatsapp_request
        auth_user = authenticate_whatsapp_request(sender)
        if not auth_user:
            logger.error(f"Auth failed for {sender}")
            return

        from whatsapp.handlers import handle_interactive_reply
        handle_interactive_reply(sender, payload, auth_user)
    except Exception as e:
        logger.error(f"Error in _handle_template_button from {sender}: {e}", exc_info=True)
        print(f"[TEMPLATE_BUTTON] EXCEPTION from {sender}: {type(e).__name__}: {e}", flush=True)


def _handle_flow_response(sender: str, interactive: dict):
    """
    Handle a WhatsApp Flow form submission (nfm_reply).
    
    The Flow form data arrives as:
      message.interactive.nfm_reply.response_json = JSON string
      containing: { resolution_rating, facility_team_rating, overall_rating, comments }
    """
    import json
    try:
        nfm_reply = interactive.get("nfm_reply", {})
        response_json_str = nfm_reply.get("response_json", "{}")
        print(f"[FLOW] Raw response_json from {sender}: {response_json_str[:500]}", flush=True)
        
        # Parse the response JSON string
        if isinstance(response_json_str, str):
            response_data = json.loads(response_json_str)
        else:
            response_data = response_json_str
        
        print(f"[FLOW] Parsed response from {sender}: {response_data}", flush=True)
        logger.info(f"Flow response from {sender}: {response_data}")
        
        # Route to feedback engine
        from feedback.engine import handle_flow_response
        print(f"[FLOW] Calling handle_flow_response for {sender}...", flush=True)
        handled = handle_flow_response(sender, response_data)
        
        if handled:
            print(f"[FLOW] ✅ Flow response processed successfully for {sender}", flush=True)
            logger.info(f"Flow response processed successfully for {sender}")
        else:
            print(f"[FLOW] ⚠️ No active session found for {sender}", flush=True)
            logger.warning(f"Flow response from {sender} — no active session found")
    
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse Flow response JSON from {sender}: {e}")
        print(f"[FLOW] JSON PARSE ERROR from {sender}: {e}", flush=True)
    except ImportError as e:
        logger.warning(f"Feedback module not available — cannot process Flow response: {e}")
        print(f"[FLOW] IMPORT ERROR: {e}", flush=True)
    except Exception as e:
        logger.error(f"Error handling Flow response from {sender}: {e}", exc_info=True)
        print(f"[FLOW] EXCEPTION from {sender}: {type(e).__name__}: {e}", flush=True)


def _handle_text(sender: str, text: str, voice_note: bool = False):
    """
    Route text messages:
      1. Check for UNDO command (voice note context)
      2. Check for Master Team Router (Facilities / Elara Home / Kanav)
      3. If sender is in a WA task-assignment state → task_assignment module
      4. For voice notes: check if batch update (multiple tasks) → batch handler
      5. Otherwise → core bot engine with a WhatsApp-native send_reply_func
    """
    # Master Team Router check (Facilities / Elara Home / Kanav)
    try:
        from elara.team_router import route_incoming_message
        if route_incoming_message(sender, text=text):
            return
    except Exception as team_err:
        logger.error(f"Team router error in _handle_text: {team_err}", exc_info=True)

    # ── Factech Client Automation Check ─────────────────────────────────────
    try:
        from clients.config import TREAT_CHAITANYA_AS_TENANT_ONLY, is_chaitanya, is_developer_phone
        from clients.flows import (
            has_active_client_flow,
            handle_client_text,
            is_registered_client,
            handle_client_button_reply,
            handle_client_hi,
        )
        from elara.team_router import get_active_team
        is_client = (
            (TREAT_CHAITANYA_AS_TENANT_ONLY and is_chaitanya(sender))
            or (is_developer_phone(sender) and get_active_team(sender) == "factech")
            or is_registered_client(sender)
        )

        if has_active_client_flow(sender):
            if handle_client_text(sender, text):
                return

        # Direct client text commands
        if is_client:
            import re
            clean_cmd = re.sub(r'[^\w\s]', '', text).strip().lower()
            if clean_cmd in ("1", "log new complaint", "log complaint", "new complaint", "create complaint", "raise complaint"):
                handle_client_button_reply(sender, "log_new_complaint")
                return
            elif clean_cmd in (
                "2",
                "select complaints",
                "select complaint",
                "complaints list",
                "view complaints",
                "my complaints",
                "complaints",
                "select",
                "check complaint status",
                "check status",
                "complaint status",
                "status",
                "active complaints",
            ):
                handle_client_button_reply(sender, "select_complaints")
                return
            elif clean_cmd in ("3", "complaint history", "history", "previous complaints", "all complaints"):
                handle_client_button_reply(sender, "complaint_history")
                return
            elif clean_cmd in (
                "4",
                "5",
                "feedback",
                "give feedback",
                "feedback on closed",
                "feedback on closed complaints",
                "give feedback on closed complaints",
                "closed complaints feedback",
                "closed feedback",
                "closed complaints",
            ):
                handle_client_button_reply(sender, "feedback_closed_complaints")
                return
            elif clean_cmd in ("update complaint", "update", "edit complaint", "modify complaint", "update ticket"):
                handle_client_button_reply(sender, "update_complaint")
                return
            elif any(clean_cmd == g or clean_cmd.startswith(f"{g} ") for g in ("6", "menu", "main menu", "hi", "hello", "hey", "help", "factech", "support")):
                handle_client_hi(sender)
                return
            else:
                logger.info(f"Client {sender} sent unrouted text '{text[:60]}' — showing client menu and assistance")
                handle_client_hi(sender)
                return
    except Exception as client_err:
        logger.error(f"Client routing error in _handle_text: {client_err}", exc_info=True)

    # Facilities routing check
    try:
        from facilities.flows.router import is_facilities_user, get_session, route_facilities_message
        session = get_session(sender)
        if is_facilities_user(sender) or (session and session.get("current_flow_state")):
            from facilities.auth import resolve_facilities_user
            fac_u = resolve_facilities_user(sender)
            route_facilities_message(sender, text=text, user=fac_u)
            return
    except Exception as fac_err:
        logger.error(f"Facilities routing error in _handle_text: {fac_err}", exc_info=True)



    # Check for clear/reset command (works from both text and voice)
    if text.strip().upper() in ("CLEAR", "CLEAR CHAT", "RESET", "CLEAR SESSION", "RESTART"):
        try:
            from feedback.session_store import force_clear_and_process_next
            from core.conversation_state import clear_state
            from core.context_manager import clear_context
            
            force_clear_and_process_next(sender)
            clear_state(sender)
            clear_context(sender)
            send_text(sender, "Chat history and active feedback sessions have been cleared! 🧹")
            return
        except Exception as clear_err:
            logger.error(f"Error clearing WhatsApp state in _handle_text for {sender}: {clear_err}")

    # Check for admin manual sync command
    if text.strip().lower() in ("sync clients", "refresh clients", "sync gsheet", "sync sheet"):
        try:
            from auth.middleware import authenticate_whatsapp_request
            from clients.sync_engine import force_client_master_sync
            u = authenticate_whatsapp_request(sender)
            if u and (u.get("role") in ("Developer", "Director", "Employee") or u.get("department") == "Facilities"):
                stats = force_client_master_sync()
                valid = stats.get("valid_records", 0)
                invalid = stats.get("invalid_records", 0)
                send_text(sender, f"🔄 *Client Master Sync complete!*\n\n✅ Valid active clients: {valid}\n⚠️ Invalid rows: {invalid}\n\nClient records are fully up to date with Google Sheets MASTER tab.")
                return
        except Exception as sync_cmd_err:
            logger.error(f"Error handling admin sync command: {sync_cmd_err}")

    # 0. Check for UNDO command (works from both text and voice)
    if text.strip().upper() == "UNDO" and not voice_note:
        _handle_voice_undo(sender)
        return

    # Intercept text commands that map directly to Main Menu button options
    clean_text = text.strip().lower()

    # Create task matching (including typos like 'create taks', 'create a task', 'new task')
    is_create_task_cmd = (
        clean_text in ["create task", "create_task", "create taks", "new task", "new_task", "add task", "create a task"] or
        any(x in clean_text for x in ["create task", "create taks", "new task", "add task", "create a task"])
    ) and not any(x in clean_text for x in ["update", "complete", "delete", "detail"])

    if is_create_task_cmd:
        from auth.middleware import authenticate_whatsapp_request
        from whatsapp.handlers import handle_interactive_reply
        user_info = authenticate_whatsapp_request(sender)
        handle_interactive_reply(sender, "menu_create_task", user_info or {})
        return

    # Update task matching (including 'update task', 'update taks', 'task update')
    is_update_task_cmd = (
        clean_text in ["update task", "update_task", "update taks", "task update", "task_update", "update a task"] or
        any(x in clean_text for x in ["update task", "update taks", "task update"])
    ) and not any(x in clean_text for x in ["create", "new", "add", "delete", "detail"])

    if is_update_task_cmd:
        from auth.middleware import authenticate_whatsapp_request
        from whatsapp.handlers import handle_interactive_reply
        user_info = authenticate_whatsapp_request(sender)
        handle_interactive_reply(sender, "menu_update_task", user_info or {})
        return

    if clean_text in ["analytics", "show analytics", "view analytics", "team analytics"]:
        from auth.middleware import authenticate_whatsapp_request
        from whatsapp.handlers import handle_interactive_reply
        user_info = authenticate_whatsapp_request(sender)
        handle_interactive_reply(sender, "menu_analytics", user_info or {})
        return

    if clean_text in ["report", "reports", "daily report", "get report", "send report"]:
        from auth.middleware import authenticate_whatsapp_request
        from whatsapp.handlers import handle_interactive_reply
        user_info = authenticate_whatsapp_request(sender)
        handle_interactive_reply(sender, "menu_reports", user_info or {})
        return

    # 1. Check WA State Machine for Multi-Step flows
    from db import supabase
    state_res = supabase.table("wa_task_states").select("*").eq("whatsapp_number", sender).execute()
    if state_res.data:
        wa_state = state_res.data[0]
        action = wa_state.get("action")
        task_id = wa_state.get("task_id")
        
        if action in ["WAITING_FOR_NOTE", "WAITING_FOR_UPDATE_NOTE", "task_update_note"]:
            clean_t = text.strip().lower()
            import json as _json
            raw_meta = wa_state.get("metadata") or {}
            if isinstance(raw_meta, str):
                try: raw_meta = _json.loads(raw_meta)
                except Exception: raw_meta = {}
            
            tid = task_id or raw_meta.get("task_id")
            tname = raw_meta.get("task_name", "Task")
            initial_n = raw_meta.get("initial_note")

            if clean_t in ["no", "n", "skip", "none", "nope", "cancel"]:
                supabase.table("wa_task_states").delete().eq("whatsapp_number", sender).execute()
                send_text(sender, f"✅ Task update completed for *'{tname}'*!")
                return

            from tasks.timeline import add_timeline_event
            user_id = supabase.table("users").select("id").eq("whatsapp_number", sender).execute().data[0]["id"]
            if tid:
                add_timeline_event(tid, user_id, "Note added to task update", note=text.strip())
                try:
                    t_row = supabase.table("tasks").select("notes").eq("id", tid).execute()
                    exist_n = t_row.data[0].get("notes") if t_row.data else ""
                    if not exist_n and initial_n:
                        exist_n = initial_n
                    full_n = f"{exist_n}\n{text.strip()}".strip() if exist_n else text.strip()
                    supabase.table("tasks").update({"notes": full_n}).eq("id", tid).execute()
                except Exception:
                    pass
            supabase.table("wa_task_states").delete().eq("whatsapp_number", sender).execute()
            send_text(sender, f"📝 Note added to task *'{tname}'* successfully! ✅")
            return
        elif action == "WAITING_FOR_REMINDER":
            try:
                from tasks.service import orchestrate_set_reminder
                from core.utils import parse_human_date
                user_id = supabase.table("users").select("id").eq("whatsapp_number", sender).execute().data[0]["id"]
                
                parsed_time = parse_human_date(text)
                if not parsed_time:
                    send_text(sender, "Couldn't understand the time format. Try 'tomorrow at 10am' or '2026-07-08 10:00'.")
                    return
                    
                orchestrate_set_reminder({"id": user_id}, task_id, parsed_time)
                supabase.table("wa_task_states").delete().eq("whatsapp_number", sender).execute()
                send_text(sender, f"✅ Reminder set successfully for: {parsed_time}")
            except Exception as e:
                send_text(sender, f"Failed to set reminder: {e}")
            return
            
        elif action in ["WAITING_FOR_PERSONAL_TASK_TITLE", "WAITING_FOR_TEAM_TASK_TITLE"]:
            is_personal = (action == "WAITING_FOR_PERSONAL_TASK_TITLE")
            task_type = "PERSONAL" if is_personal else "TEAM"
            task_title = text.strip()
            
            meta = {
                "task_title": task_title,
                "is_personal": is_personal,
                "task_type": task_type
            }
            from whatsapp.handlers import set_wa_state
            set_wa_state(sender, "WAITING_FOR_TASK_START_DATE", metadata=meta)
            send_text(sender, f"What is the Start Date for *'{task_title}'*?\n(e.g., *today*, *tomorrow*, *next Monday*, or *2026-08-10*)")
            return

        elif action == "WAITING_FOR_TASK_START_DATE":
            from core.utils import parse_human_date
            import json as _json
            
            raw_meta = wa_state.get("metadata") or {}
            if isinstance(raw_meta, str):
                try: raw_meta = _json.loads(raw_meta)
                except Exception: raw_meta = {}
                
            parsed_start = parse_human_date(text)
            raw_meta["start_date"] = parsed_start
            
            task_title = raw_meta.get("task_title", "Task")
            from whatsapp.handlers import set_wa_state
            set_wa_state(sender, "WAITING_FOR_TASK_DEADLINE", metadata=raw_meta)
            send_text(sender, f"What is the Deadline for *'{task_title}'*?\n(e.g., *tomorrow*, *next Friday*, *in 1 week*, or *2026-08-15*)")
            return

        elif action == "WAITING_FOR_TASK_DEADLINE":
            supabase.table("wa_task_states").delete().eq("whatsapp_number", sender).execute()
            from core.utils import parse_human_date, format_date_human
            import json as _json
            
            raw_meta = wa_state.get("metadata") or {}
            if isinstance(raw_meta, str):
                try: raw_meta = _json.loads(raw_meta)
                except Exception: raw_meta = {}
                
            task_title = raw_meta.get("task_title", text)
            is_personal = raw_meta.get("is_personal", True)
            task_type = raw_meta.get("task_type", "PERSONAL")
            start_date = raw_meta.get("start_date")
            parsed_dl = parse_human_date(text)
            
            try:
                from db import add_task
                from auth.middleware import authenticate_whatsapp_request
                
                user_info = authenticate_whatsapp_request(sender)
                user_id = user_info["id"] if user_info else None
                team_id = user_info.get("team_id") if user_info else None
                
                new_task = add_task(
                    project_id=None,
                    name=task_title,
                    deadline=parsed_dl,
                    start_date=start_date,
                    assigned_by=user_id,
                    created_by=user_id,
                    assigned_to=user_id if is_personal else None,
                    task_type=task_type,
                    team_id=team_id
                )
                
                if new_task:
                    f_start = format_date_human(start_date)
                    f_dl = format_date_human(parsed_dl)
                    if is_personal:
                        send_text(sender, f"✅ Personal Task *'{task_title}'* created successfully!\n📅 Start Date: {f_start}\n📅 Deadline: {f_dl}")
                    else:
                        from whatsapp.handlers import send_assignee_selection_prompt
                        send_assignee_selection_prompt(sender, new_task['id'], task_title, user_info or {})
                else:
                    send_text(sender, "Failed to create task in the database. Contact an admin.")
            except Exception as e:
                logger.error(f"Error creating task: {e}")
                send_text(sender, "An error occurred while creating the task.")
            return

        elif action == "WAITING_FOR_UPDATE_DATE_TASK_SELECTION":
            from auth.middleware import authenticate_whatsapp_request
            from db import supabase
            from core.context_manager import get_context
            from core.utils import resolve_task_from_list
            import re
            
            user_info = authenticate_whatsapp_request(sender)
            user_id = user_info["id"] if user_info else None
            
            ctx = get_context(sender)
            task_ids = ctx.get("last_task_list", [])
            if not task_ids and user_id:
                task_ids = get_context(user_id).get("last_task_list", [])
                
            all_raw = supabase.table("tasks").select("*, projects(name)").execute().data or []
            if task_ids:
                tasks = [t for t in all_raw if t["id"] in task_ids]
            else:
                tasks = [t for t in all_raw if t.get("status") != "Completed"]
                
            target_task = None
            num_match = re.search(r'^(?:task|number|#)?\s*(\d+)', text.strip(), re.IGNORECASE)
            if num_match:
                idx = int(num_match.group(1)) - 1
                if 0 <= idx < len(tasks):
                    target_task = tasks[idx]
            if not target_task:
                target_task = resolve_task_from_list(text, tasks, last_list_ids=task_ids)
                
            if not target_task:
                send_text(sender, "Could not identify task. Please reply with the task number (e.g. *1*).")
                return
                
            task_id = target_task["id"]
            task_name = target_task.get("title") or target_task.get("name") or "Task"
            
            supabase.table("wa_task_states").delete().eq("whatsapp_number", sender).execute()
            
            body = f"What date would you like to update for *{task_name}*?"
            buttons = [
                {"id": f"date_type_start_{task_id}", "title": "Start Date"},
                {"id": f"date_type_dl_{task_id}", "title": "Deadline"}
            ]
            from whatsapp.ux import send_interactive_buttons
            send_interactive_buttons(sender, body, buttons)
            return

        elif action == "WAITING_FOR_NEW_START_DATE":
            supabase.table("wa_task_states").delete().eq("whatsapp_number", sender).execute()
            from core.utils import parse_human_date, format_date_human
            from db import update_task_dates
            import json as _json
            
            raw_meta = wa_state.get("metadata") or {}
            if isinstance(raw_meta, str):
                try: raw_meta = _json.loads(raw_meta)
                except Exception: raw_meta = {}
            task_id = wa_state.get("task_id") or raw_meta.get("task_id")
            
            parsed_date = parse_human_date(text)
            res = update_task_dates(task_id, start_date=parsed_date)
            if res:
                f_date = format_date_human(parsed_date)
                t_name = res.get("title") or res.get("name") or "Task"
                send_text(sender, f"✅ Start Date updated successfully for *'{t_name}'*!\n📅 New Start Date: {f_date}")
            else:
                send_text(sender, "Failed to update Start Date in database.")
            return

        elif action == "WAITING_FOR_NEW_DEADLINE":
            supabase.table("wa_task_states").delete().eq("whatsapp_number", sender).execute()
            from core.utils import parse_human_date, format_date_human
            from db import update_task_dates
            import json as _json
            
            raw_meta = wa_state.get("metadata") or {}
            if isinstance(raw_meta, str):
                try: raw_meta = _json.loads(raw_meta)
                except Exception: raw_meta = {}
            task_id = wa_state.get("task_id") or raw_meta.get("task_id")
            
            parsed_date = parse_human_date(text)
            res = update_task_dates(task_id, deadline=parsed_date)
            if res:
                f_date = format_date_human(parsed_date)
                t_name = res.get("title") or res.get("name") or "Task"
                send_text(sender, f"✅ Deadline updated successfully for *'{t_name}'*!\n📅 New Deadline: {f_date}")
            else:
                send_text(sender, "Failed to update Deadline in database.")
            return

        elif action == "WAITING_FOR_TASK_UPDATE":
            supabase.table("wa_task_states").delete().eq("whatsapp_number", sender).execute()
            from auth.middleware import authenticate_whatsapp_request
            from whatsapp.handlers import handle_direct_task_update
            
            user_info = authenticate_whatsapp_request(sender)
            if handle_direct_task_update(sender, text, user_info or {}):
                return

        elif action == "WAITING_FOR_COMPLETION_IMAGE_DECISION":
            clean_t = text.strip().lower()
            if clean_t in ["yes", "y", "yep", "ok", "sure"]:
                send_text(sender, "Please upload an image as proof of completion 📎")
                supabase.table("wa_task_states").update({"action": "WAITING_FOR_COMPLETION_IMAGE"}).eq("whatsapp_number", sender).execute()
            elif clean_t in ["no", "n", "skip", "nope"]:
                send_text(sender, "Would you like to add a final comment or note for this task? 📝\n\nReply with your comment, or send 'No' to skip.")
                supabase.table("wa_task_states").update({"action": "WAITING_FOR_COMPLETION_COMMENT"}).eq("whatsapp_number", sender).execute()
            else:
                send_text(sender, "Please reply with *Yes* to upload an image or *No* to skip.")
            return

        elif action in ["WAITING_FOR_COMPLETION_IMAGE", "WAITING_FOR_PROOF"]:
            clean_t = text.strip().lower()
            if clean_t in ["no", "n", "skip", "nope"]:
                send_text(sender, "Would you like to add a final comment or note for this task? 📝\n\nReply with your comment, or send 'No' to skip.")
                supabase.table("wa_task_states").update({"action": "WAITING_FOR_COMPLETION_COMMENT"}).eq("whatsapp_number", sender).execute()
            else:
                send_text(sender, "Please upload an image as proof of completion 📎 (or reply 'No' to skip).")
            return

        elif action == "WAITING_FOR_COMPLETION_COMMENT":
            import json as _json
            clean_t = text.strip().lower()
            final_comment = None if clean_t in ["no", "n", "skip", "none", "nope"] else text.strip()
            
            # Read image URL from metadata JSONB column
            raw_metadata = wa_state.get("metadata") or {}
            if isinstance(raw_metadata, str):
                try:
                    raw_metadata = _json.loads(raw_metadata)
                except (_json.JSONDecodeError, TypeError):
                    raw_metadata = {}
            image_url = raw_metadata.get("image_url")
            
            from auth.middleware import authenticate_whatsapp_request
            from db import save_update
            from tasks.service import orchestrate_status_update
            from tasks.timeline import add_timeline_event

            user_info = authenticate_whatsapp_request(sender)
            user_id = user_info["id"] if user_info else None

            images_list = [image_url] if image_url else []

            # Fetch task name for the final message
            task_name = "Task"
            try:
                task_row = supabase.table("tasks").select("title").eq("id", task_id).execute()
                if task_row.data:
                    task_name = task_row.data[0].get("title", "Task")
            except Exception:
                pass

            save_update(
                task_id=task_id,
                progress=100,
                blockers="None",
                images=images_list,
                employee_id=user_id,
                new_deadline=None,
                note=final_comment
            )

            orchestrate_status_update(
                current_user=user_info if user_info else {"id": user_id},
                task_id=task_id,
                new_status="Completed",
                note=final_comment or "",
                proof_url=image_url or ""
            )

            if final_comment:
                add_timeline_event(task_id, str(user_id or ""), "Final comment added on completion", note=final_comment)
                try:
                    supabase.table("tasks").update({"notes": final_comment}).eq("id", task_id).execute()
                except Exception:
                    pass

            supabase.table("wa_task_states").delete().eq("whatsapp_number", sender).execute()

            # Build final completion message
            msg = f"✅ *{task_name}* — Updated to Completed! 🎉"
            if image_url:
                msg += f"\n📸 Proof: {image_url}"
            if final_comment:
                msg += f"\n📝 Notes: {final_comment}"
            send_text(sender, msg)
            return


    # 1.5 Task assignment multi-step state (legacy fallback, to be removed)
    try:
        from whatsapp.task_assignment import handle_text_reply
        if handle_text_reply(sender, text):
            logger.info(f"Text handled by legacy task_assignment module for {sender}")
            return
    except Exception:
        pass

    # 2. Voice note batch detection — multiple tasks in one message
    if voice_note:
        try:
            from whatsapp.voice_batch_handler import is_batch_transcript, extract_batch_updates
            if is_batch_transcript(text):
                logger.info(f"Batch voice note detected from {sender}")
                batch_updates = extract_batch_updates(text)
                if batch_updates and len(batch_updates) > 1:
                    _handle_voice_batch(sender, batch_updates)
                    return
                logger.info("Batch extraction returned single/no updates, falling through to normal pipeline")
        except Exception as e:
            logger.warning(f"Batch detection failed, falling through: {e}")

    # 3. Core bot engine — pass a WA-native reply function so documents work
    source_label = "voice" if voice_note else "text"
    logger.info(f"{source_label.capitalize()} from {sender} → core engine: {text[:80]}")

    # Track pre-update state for voice note UNDO capability
    pre_update_snapshot = None
    if voice_note:
        pre_update_snapshot = _capture_pre_update_snapshot(sender, text)

    async def wa_send_reply(text: str | None = None, document: str | None = None, target_user_id: int | None = None):
        """
        WhatsApp-aware send function.
        - text messages → sent as plain text
        - document (PDF path) → uploaded to WA media API, then sent as document
        """
        if text:
            send_text(sender, text)
        if document:
            _send_document_wa(sender, document)

    try:
        asyncio.run(
            process_user_message(user_id=sender, text=text, send_reply_func=wa_send_reply)
        )

        # After successful processing, store voice action for UNDO
        if voice_note and pre_update_snapshot:
            _store_voice_action(sender, pre_update_snapshot)

    except Exception as e:
        logger.error(f"Core engine error for {sender}: {e}", exc_info=True)
        # Send a friendly message so the user isn't left hanging
        try:
            send_text(sender, friendly_system_error(text))
        except Exception:
            pass  # Last resort — can't even send the error message


def _handle_voice_batch(sender: str, updates: list):
    """
    Process multiple task updates from a single voice note.

    For each update:
      - Resolve project + task
      - If ambiguous → collect for later disambiguation
      - If completed → mark complete and queue image request
      - Otherwise → save progress update

    Sends a single combined summary at the end.
    """
    from db import (
        get_projects, get_all_tasks, save_update, complete_task,
        add_blocker as db_add_blocker, get_user_by_telegram_id
    )
    from core.utils import resolve_project, resolve_task_from_list
    from core.conversation_state import set_state

    projects = get_projects()
    all_tasks = get_all_tasks()

    # Resolve sender's DB user
    emp_uuid = None
    try:
        from whatsapp.task_assignment import get_user_by_whatsapp
        user = get_user_by_whatsapp(sender)
        if user:
            emp_uuid = user['id']
    except Exception:
        pass

    if not emp_uuid:
        try:
            u_info = get_user_by_telegram_id(int(sender))
            emp_uuid = u_info['id'] if u_info else None
        except Exception:
            pass

    saved = []        # Successfully saved updates
    completed = []    # Tasks marked complete
    ambiguous = []    # Couldn't resolve — need user input
    failed = []       # Couldn't find task at all

    for upd in updates:
        task_name = upd["task_name"]
        project_name = upd.get("project_name")
        progress = upd.get("progress")
        is_complete = upd.get("completed", False)
        blocker = upd.get("blocker")

        # Resolve project to narrow task search
        active_project_id = None
        if project_name:
            p_match = resolve_project(project_name, projects)
            if p_match:
                active_project_id = p_match['id']

        # Resolve task
        match = resolve_task_from_list(
            task_name, all_tasks, active_project_id=active_project_id
        )

        # Ambiguous? 
        amb_matches = getattr(resolve_task_from_list, "ambiguous_matches", [])
        if not match and amb_matches:
            amb_tasks = amb_matches
            ambiguous.append({
                "query": task_name,
                "options": amb_tasks,
                "progress": progress,
                "completed": is_complete,
                "blocker": blocker,
            })
            continue

        # Not found?
        if not match:
            failed.append(task_name)
            continue

        # Process the update
        prog_val = progress if progress is not None else (match.get('progress', 0) or 0)
        if is_complete:
            prog_val = 100

        blocker_text = blocker or "None"
        save_update(match['id'], prog_val, blocker_text, [], emp_uuid)

        if blocker and blocker.lower() not in ["none", "null", ""]:
            db_add_blocker(match['id'], blocker)

        if is_complete:
            complete_task(match['id'])
            p_name = ""
            if isinstance(match.get('projects'), dict):
                p_name = match['projects'].get('name', '')
            completed.append({"name": match['name'], "project": p_name, "id": match['id']})
        else:
            saved.append({
                "name": match['name'],
                "progress": prog_val,
                "blocker": blocker,
            })

    # Build summary message
    msg_parts = []

    if saved:
        msg_parts.append("✅ *Updates saved:*")
        for s in saved:
            line = f"  • {s['name']} — {s['progress']}%"
            if s.get('blocker'):
                line += f" 🛑 {s['blocker']}"
            msg_parts.append(line)

    if completed:
        msg_parts.append("\n🎉 *Marked complete:*")
        for c in completed:
            proj = f" ({c['project']})" if c.get('project') else ""
            msg_parts.append(f"  • {c['name']}{proj}")

    if failed:
        msg_parts.append("\n⚠️ *Couldn't find:*")
        for f_name in failed:
            msg_parts.append(f"  • \"{f_name}\" — check the task name")

    if not msg_parts:
        send_text(sender, "Couldn't process any updates from your voice note. Please try again.")
        return

    summary = "\n".join(msg_parts)
    send_text(sender, summary)

    # Handle ambiguous tasks — ask for each one
    if ambiguous:
        # Queue the first ambiguous item for disambiguation
        first = ambiguous[0]
        amb_tasks = first["options"]
        msg = f"\n\nMultiple tasks match *\"{first['query']}\"*. Which one?\n\n"
        for i, t in enumerate(amb_tasks, 1):
            p_name = t.get('projects', {}).get('name', '') if isinstance(t.get('projects'), dict) else ''
            msg += f"{i}. {t['name']}" + (f" ({p_name})" if p_name else "") + "\n"
        msg += "\nReply with the number."

        set_state(sender, {
            "action": "disambiguate_update",
            "step": "waiting_for_choice",
            "task_options": [t['id'] for t in amb_tasks],
            "task_names": [t['name'] for t in amb_tasks],
            "progress_str": str(first.get("progress")) if first.get("progress") is not None else None,
            "deadline": None,
            "images": [],
            # Queue remaining ambiguous items
            "pending_ambiguous": ambiguous[1:] if len(ambiguous) > 1 else [],
        })
        send_text(sender, msg)

    # Ask for proof images if any tasks were completed
    elif completed:
        comp_names = ", ".join([c['name'] for c in completed])
        set_state(sender, {
            "action": "voice_batch_image_confirm",
            "step": "waiting_for_choice",
            "completed_task_ids": [c['id'] for c in completed],
            "completed_task_names": [c['name'] for c in completed],
        })
        send_text(
            sender,
            f"\n📸 *{comp_names}* marked complete.\n"
            f"Do you want to add proof images?\n\n"
            f"1. Yes\n2. No"
        )


# ─────────────────────────────────────────────────────────────────────────────
# VOICE NOTE HANDLER
# ─────────────────────────────────────────────────────────────────────────────

def _handle_audio(sender: str, message: dict):
    """
    Handle an incoming voice note (audio message) from WhatsApp.

    Pipeline:
      1. Download & transcribe via audio_handler
      2. On failure → send error-specific friendly reply
      3. On success → send acknowledgement with transcript
      4. Check for UNDO command in transcript
      5. Feed transcript into the SAME _handle_text pipeline as typed messages
    """
    try:
        from elara.auth import is_elara_user
        auth_user = None
        if not is_elara_user(sender):
            from auth.middleware import authenticate_whatsapp_request
            auth_user = authenticate_whatsapp_request(sender)
    except Exception as e:
        logger.error(f"Auth middleware error: {e}")

    media_id = message.get("audio", {}).get("id")
    if not media_id:
        logger.warning(f"Audio message from {sender} has no media ID")
        send_text(sender, "🎙️ Couldn't process your voice note — no audio found.")
        return

    logger.info(f"Voice note received from {sender}, media_id: {media_id}")

    # Lazy import — avoid loading groq SDK at server startup
    from whatsapp.audio_handler import handle_voice_note

    # Step 1: Download + Transcribe
    result = handle_voice_note(media_id, sender)

    # Step 2: Handle errors with specific friendly messages
    if not result["success"]:
        error_type = result.get("error_type", "unknown")
        logger.warning(f"Voice note failed for {sender}: {error_type} — {result.get('error')}")

        error_messages = {
            "download_failed": (
                "🎙️ Couldn't download your voice note. "
                "WhatsApp sometimes delays audio delivery. "
                "Please resend or type your update."
            ),
            "transcription_failed": (
                "🎙️ Transcription failed right now. "
                "Please type your update — "
                "voice notes will be back shortly."
            ),
            "rate_limit": (
                "🎙️ Transcription failed right now. "
                "Please type your update — "
                "voice notes will be back shortly."
            ),
            "empty_transcript": (
                "🎙️ Couldn't hear anything in your voice note. "
                "Try recording again — speak clearly and close to the mic."
            ),
        }

        reply = error_messages.get(error_type, (
            "🎙️ Sorry, I couldn't process your voice note. "
            "Please try again or type your update instead."
        ))
        send_text(sender, reply)
        return

    transcript = result["transcript"]
    logger.info(f"Voice note transcribed for {sender}: {transcript[:100]}")

    # Step 3: Send immediate acknowledgement BEFORE processing
    send_text(
        sender,
        f"🎙️ Got your voice note. Processing...\n\n"
        f"_I heard:_ \"{transcript}\""
    )

    # Step 4: Check for Master Team Router (Facilities / Elara Home / Kanav)
    try:
        from elara.team_router import route_incoming_message
        if route_incoming_message(sender, voice_transcript=transcript):
            return
    except Exception as team_err:
        logger.error(f"Voice team routing error: {team_err}", exc_info=True)

    # Step 5: Check for UNDO command
    if transcript.strip().upper() == "UNDO":
        _handle_voice_undo(sender)
        return

    # Step 6: Feed transcript into the SAME pipeline as typed messages
    _handle_text(sender, transcript, voice_note=True)


def _handle_image(sender: str, message: dict, media_type: str = "image"):
    """
    Handles incoming image and document messages from WhatsApp (msg_type in ('image', 'document')).
    Routes to Elara Home or Facilities depending on active session state or team affiliation.
    If the user is in a legacy task completion / proof upload state, saves the image URL
    in metadata and advances state to WAITING_FOR_COMPLETION_COMMENT.
    """
    import config
    try:
        from auth.middleware import authenticate_whatsapp_request
        auth_user = authenticate_whatsapp_request(sender)
        if not auth_user:
            logger.error(f"Auth failed for {sender}")
            return
    except Exception as e:
        logger.error(f"Auth middleware error in _handle_image: {e}")
        return

    media_obj = message.get(media_type, {})
    media_id = media_obj.get("id")
    caption = media_obj.get("caption", "").strip()

    if not media_id:
        logger.warning(f"Media message ({media_type}) from {sender} has no media ID")
        return

    # ── Media routing (Facilities vs Elara Home) ──
    fac_session = None
    elara_session = None
    active_team = None
    try:
        from facilities.flows.router import is_facilities_user, get_session as get_fac_session, route_facilities_message
        from elara.flows.router import route_elara_image
        from elara.session import get_elara_session
        from elara.auth import is_elara_user
        from elara.team_router import get_active_team

        fac_session = get_fac_session(sender)
        elara_session = get_elara_session(sender)
        active_team = get_active_team(sender)

        # 1. Check active in-progress attachment sessions first (prevents cross-team collision for dual users)
        if elara_session and elara_session.get("flow_state", "").startswith("attach_"):
            route_elara_image(sender, image_data=media_obj, user=auth_user, session=elara_session)
            return

        if fac_session and fac_session.get("current_flow_state", "").startswith("attach_"):
            route_facilities_message(sender, image_data=media_obj, user=auth_user)
            return

    except Exception as route_err:
        logger.error(f"Media routing error for {sender}: {route_err}", exc_info=True)

    logger.info(f"Media ({media_type}) received from {sender}, media_id: {media_id}")

    # Resolve image URL via Meta Graph API
    media_url = None
    graph_version = getattr(config, "GRAPH_API_VERSION", "v19.0")
    try:
        import requests
        url_endpoint = f"https://graph.facebook.com/{graph_version}/{media_id}"
        headers = {"Authorization": f"Bearer {config.META_ACCESS_TOKEN}"}
        resp = requests.get(url_endpoint, headers=headers, timeout=10)
        if resp.status_code == 200:
            media_url = resp.json().get("url")
    except Exception as err:
        logger.error(f"Error fetching image URL for media_id {media_id}: {err}")

    # Use media_url if resolved, else store the media_id reference
    img_ref = media_url or f"wa_media:{media_id}"

    # Check WA State Machine for Multi-Step flows
    from db import supabase
    import json
    state_res = supabase.table("wa_task_states").select("*").eq("whatsapp_number", sender).execute()
    if state_res.data:
        wa_state = state_res.data[0]
        action = wa_state.get("action")

        if action in ["WAITING_FOR_COMPLETION_IMAGE_DECISION", "WAITING_FOR_COMPLETION_IMAGE", "WAITING_FOR_PROOF"]:
            # Store image URL in metadata JSONB column and transition to comment step
            existing_metadata = wa_state.get("metadata") or {}
            if isinstance(existing_metadata, str):
                try:
                    existing_metadata = json.loads(existing_metadata)
                except (json.JSONDecodeError, TypeError):
                    existing_metadata = {}
            existing_metadata["image_url"] = img_ref

            supabase.table("wa_task_states").update({
                "action": "WAITING_FOR_COMPLETION_COMMENT",
                "metadata": json.dumps(existing_metadata)
            }).eq("whatsapp_number", sender).execute()

            send_text(
                sender,
                "Image proof received! 📸\n\n"
                "Would you like to add a final comment or note for this task? 📝\n\n"
                "Reply with your comment, or send *No* to skip."
            )
            return

    # Team fallback when not in an active flow
    try:
        from facilities.flows.router import is_facilities_user, route_facilities_message
        from elara.flows.router import route_elara_image
        from elara.auth import is_elara_user

        if active_team == "elara" or (is_elara_user(auth_user) and not is_facilities_user(sender)):
            route_elara_image(sender, image_data=media_obj, user=auth_user, session=elara_session)
            return
        elif active_team == "facilities" or is_facilities_user(sender):
            route_facilities_message(sender, image_data=media_obj, user=auth_user)
            return
    except Exception as fb_err:
        logger.error(f"Team media fallback error for {sender}: {fb_err}")

    send_text(sender, "📸 Image received! If you are updating a task, please select the task update flow first.")



def _handle_document(sender: str, message: dict):
    """
    Handles incoming document messages from WhatsApp (msg_type == 'document').
    Delegates to _handle_image pipeline with media_type='document'.
    """
    _handle_image(sender, message, media_type="document")


def _handle_voice_undo(sender: str):
    """
    Handle UNDO command from a voice note or text.
    Reverts the last voice-note update if within 5 minutes.
    """
    try:
        from whatsapp.task_assignment import get_user_by_whatsapp
        from db import supabase
        from datetime import datetime, timedelta
        import json

        user = get_user_by_whatsapp(sender)
        if not user:
            send_text(sender, "Couldn't identify you — please contact Kanav.")
            return

        # Check wa_task_states for last_voice_action
        try:
            r = supabase.table("wa_task_states").select("last_voice_action").eq(
                "whatsapp_number", sender
            ).execute()
            state_row = r.data[0] if r.data else None
        except Exception:
            state_row = None

        if not state_row or not state_row.get("last_voice_action"):
            send_text(sender, "No recent voice update to undo.")
            return

        voice_action = state_row["last_voice_action"]
        if isinstance(voice_action, str):
            voice_action = json.loads(voice_action)

        # Check 5-minute window
        action_ts = voice_action.get("timestamp")
        if action_ts:
            action_time = datetime.fromisoformat(action_ts)
            if datetime.now() - action_time > timedelta(minutes=5):
                send_text(sender, "⏰ UNDO window expired (5 minutes). The update cannot be reversed.")
                return

        task_id = voice_action.get("task_id")
        prev_progress = voice_action.get("prev_progress")
        update_id = voice_action.get("update_id")

        if not task_id:
            send_text(sender, "No recent voice update to undo.")
            return

        # Revert: delete the update record and restore task progress
        if update_id:
            try:
                supabase.table("updates").delete().eq("id", update_id).execute()
            except Exception as e:
                logger.warning(f"Could not delete update {update_id}: {e}")

        if prev_progress is not None:
            supabase.table("tasks").update(
                {"progress": prev_progress}
            ).eq("id", task_id).execute()

        # Clear the voice action
        supabase.table("wa_task_states").update(
            {"last_voice_action": None}
        ).eq("whatsapp_number", sender).execute()

        send_text(sender, "↩️ Done. Last voice update reversed.")
        logger.info(f"UNDO completed for {sender}, task {task_id}")

    except Exception as e:
        logger.error(f"UNDO failed for {sender}: {e}", exc_info=True)
        send_text(sender, "Couldn't undo right now — please try again.")

# ─────────────────────────────────────────────────────────────────────────────
# VOICE NOTE UNDO HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _capture_pre_update_snapshot(sender: str, transcript: str):
    """
    Before processing a voice note, capture the current state of the
    most likely task being updated. This allows UNDO to restore it.

    Returns a dict with task_id, prev_progress, or None if we can't
    determine which task is being referenced.
    """
    try:
        from whatsapp.task_assignment import get_user_by_whatsapp
        from db import supabase, get_all_tasks

        user = get_user_by_whatsapp(sender)
        if not user:
            return None

        # Fetch all tasks assigned to this user
        tasks = get_all_tasks()
        user_tasks = [t for t in tasks if t.get('assigned_to') == user['id']
                      and t.get('status') != 'completed']

        if not user_tasks:
            return None

        # Try to fuzzy-match a task from the transcript
        from core.utils import resolve_task_from_list
        match = resolve_task_from_list(transcript, tasks, active_project_id=None)

        if match:
            # Get the latest update for this task to find update_id
            latest = supabase.table("updates").select("id").eq(
                "task_id", match["id"]
            ).order("timestamp", desc=True).limit(1).execute()

            return {
                "task_id": match["id"],
                "task_name": match.get("name"),
                "prev_progress": match.get("progress", 0) or 0,
                "latest_update_id": latest.data[0]["id"] if latest.data else None,
            }

        # If no match found, just store the first active task as fallback
        # The actual update ID will be captured post-processing
        return {
            "task_id": None,
            "prev_progress": None,
            "latest_update_id": None,
        }

    except Exception as e:
        logger.warning(f"Pre-update snapshot failed for {sender}: {e}")
        return None


def _store_voice_action(sender: str, snapshot: dict):
    """
    After a voice note is processed, store the action details in
    wa_task_states.last_voice_action for UNDO capability.

    The snapshot is enriched with the latest update_id (created by
    the update engine) and a timestamp.
    """
    try:
        from db import supabase
        from datetime import datetime
        import json

        if not snapshot or not snapshot.get("task_id"):
            # Try to get the task_id from the most recent update
            # by this user in the last 10 seconds
            from whatsapp.task_assignment import get_user_by_whatsapp
            user = get_user_by_whatsapp(sender)
            if user:
                latest = supabase.table("updates").select("id, task_id, progress").eq(
                    "employee_id", user["id"]
                ).order("timestamp", desc=True).limit(1).execute()
                if latest.data:
                    snapshot = snapshot or {}
                    snapshot["task_id"] = latest.data[0]["task_id"]
                    snapshot["update_id"] = latest.data[0]["id"]
                    if snapshot.get("prev_progress") is None:
                        snapshot["prev_progress"] = 0

        if not snapshot or not snapshot.get("task_id"):
            return

        # Get the actual new update_id (the one just created)
        from whatsapp.task_assignment import get_user_by_whatsapp
        user = get_user_by_whatsapp(sender)
        if user and not snapshot.get("update_id"):
            latest = supabase.table("updates").select("id").eq(
                "task_id", snapshot["task_id"]
            ).order("timestamp", desc=True).limit(1).execute()
            if latest.data:
                snapshot["update_id"] = latest.data[0]["id"]

        voice_action = {
            "task_id": snapshot.get("task_id"),
            "prev_progress": snapshot.get("prev_progress"),
            "update_id": snapshot.get("update_id") or snapshot.get("latest_update_id"),
            "timestamp": datetime.now().isoformat(),
        }

        # Upsert into wa_task_states
        supabase.table("wa_task_states").upsert({
            "whatsapp_number": sender,
            "last_voice_action": json.dumps(voice_action),
        }, on_conflict="whatsapp_number").execute()

        logger.info(f"Stored voice action for UNDO: {sender} → task {voice_action['task_id']}")

    except Exception as e:
        logger.warning(f"Failed to store voice action for {sender}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# LOCAL RUN
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)