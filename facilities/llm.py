from __future__ import annotations

"""
Facilities Module — LLM Integration (Groq / Gemini)
=====================================================
Uses Groq or Gemini API for:
  - Intent extraction from free text and voice transcriptions
  - Entity extraction (building, type, dates, owner, status, ref_no)
  - Generating conversational replies matching the mockup tone
  - Duplicate detection via semantic similarity

Fallback to rule-based parser if LLM APIs are unreachable.
"""

import logging
import json
import requests
from typing import Optional

from facilities.config import (
    GEMINI_API_KEY,
    GROQCLOUD_API_KEY,
    LLM_PROVIDER,
    FACILITIES_GROQ_MODEL,
    FACILITIES_GROQ_FALLBACK_MODELS,
    FACILITIES_GEMINI_MODEL,
    VALID_TASK_TYPES,
    VALID_STATUSES,
    BUILDING_TABS,
)

logger = logging.getLogger(__name__)

# ── System Prompts ───────────────────────────────────────────────────────────

FACILITIES_SYSTEM_PROMPT_BODY = """
Your job is to:
1. Extract the user's INTENT from their message
2. Extract relevant ENTITIES (building, type, dates, owner, status, ref_no, employee_name, etc.)
3. Return structured JSON

ALLOWED INTENTS:
- create_task: User wants to create a new facilities task
- update_task: User wants to update an existing task (status, notes, etc.)
- view_my_tasks: User wants to see their own tasks
- view_team_tasks: User wants to see team tasks for a building
- view_task_detail: User wants to see full details of a specific task
- view_summary: User wants to see a summary/rollup of tasks
- view_sync_status: User wants to check Google Sheets sync health
- view_history: User wants to see the history of a specific task
- reassign_task: User wants to reassign a task to someone else
- attach_file: User wants to attach a file to a task
- filter_building: User wants to filter by a specific building
- show_overdue_tasks: User wants to see overdue tasks (e.g., "show overdue tasks", "what tasks are overdue", "show overdue")
- filter_tasks: User wants to filter/view tasks by criteria (employee name, status, date range, etc.)
  Examples: "show Vikash tasks", "show pending tasks", "show tasks between 20 and 29 Aug",
            "show Vikramjeet's overdue tasks in GEBB1", "show future tasks for GETT"
- daily_digest: User wants their daily activity digest
- greeting: Simple hello/hi
- help: User needs help or guidance
- unknown: Cannot determine intent

IMPORTANT CONTEXT — OWNER POSITION vs EMPLOYEE NAME:
The Google Sheet "Owner" column contains POSITION TITLES, not employee names.
Known positions and their mapped employees:
- "Facility Head" → Anoop
- "Facility Manager" / "Facility Manager GEBB1" / "Facility Manager GEBB2" → Vikramjeet (also known as Vikram)
- "Facility Manager" / "Facility Manager GETT" → Vikash
- "Facilities Director" → Kanav (also known as KK, Director)

When users refer to employees by name (e.g., "created by Kanav", "by kk", "assigned to Anoop"), extract owner = the matching position title (e.g., "Facilities Director", "Facility Head", "Facility Manager") and clean the task description to not duplicate the author attribution if desired.

ENTITY EXTRACTION:
- building: One of GEBB1, GEBB2, GETT, Common (fuzzy match from aliases like "bay 1" → GEBB1, "tech tower" or "getting" → GETT)
- type: One of: Project, Client Escalation, Management Discussion, Improvement / Initiative, Major Concern, Other
- issue_action: The task description/action to be taken (e.g. "Test task", "Check AC cooling", "Stack parking civil work")
- owner: Position name to assign to ("Facilities Director", "Facility Head", "Facility Manager")
- employee_name: Person name (e.g., "Vikash", "Vikramjeet", "Anoop", "Kanav", "Abhijeet")
- target_date: Due date (parse natural language dates like "tomorrow", "next Friday" to YYYY-MM-DD)
- status: One of Open, WIP, On Hold, Closed, or descriptive terms like "pending" (→ Open), "in progress" (→ WIP), "completed" (→ Closed)
- ref_no: Task reference number (e.g., GEBB1-001, GETT-042)
- latest_update: Free text update/note about the task
- overdue: Boolean — true if user is asking about overdue tasks
- future: Boolean — true if user is asking about future/upcoming tasks
- date_range_start: Start date for date range queries (YYYY-MM-DD)
- date_range_end: End date for date range queries (YYYY-MM-DD)
- date_field: Which date field to filter on — "target_date" (default, for due-date queries) or "created_date" (for "raised" queries)

IMPORTANT RULES:
- Extract as many entities as you can from the message
- If a field is not mentioned, set it to null
- For dates, normalize to YYYY-MM-DD format strictly relative to the CURRENT SYSTEM DATE provided above
- For building names, always resolve to the standard code (GEBB1, GEBB2, GETT, Common). If user says "getting" or "tech tower", resolve to GETT
- If the user provides a sentence like "Test task created by Kanav" or "dummy task created by kk", extract issue_action = "Test task" (or the full description) and owner = "Facilities Director" (Kanav's position)
- ref_no must match the pattern BUILDING-NNN
- If multiple intents are present (e.g., voice note with multiple tasks), return them all in the intents array
- Set confidence 0.0–1.0 based on how certain you are
- "Vikram" and "Vikramjeet" are the same person — normalize to "Vikramjeet"
- "Kanav" and "KK" are the same person — normalize to "Facilities Director"
- "show overdue" or "overdue tasks" → intent = show_overdue_tasks
- "show pending tasks" or "show Vikash tasks" → intent = filter_tasks
- "show overdue tasks in GEBB1" → intent = show_overdue_tasks with building = GEBB1

OUTPUT FORMAT (JSON only):
{
  "intents": [
    {
      "intent": "filter_tasks",
      "confidence": 0.95,
      "entities": {
        "building": "GEBB1",
        "type": null,
        "issue_action": null,
        "owner": null,
        "employee_name": "Vikramjeet",
        "target_date": null,
        "status": null,
        "ref_no": null,
        "latest_update": null,
        "overdue": true,
        "future": false,
        "date_range_start": null,
        "date_range_end": null,
        "date_field": "target_date"
      }
    }
  ]
}
"""


def _get_facilities_system_prompt() -> str:
    """Build system prompt injecting the exact current date/year."""
    from datetime import datetime
    now_dt = datetime.now()
    today_str = now_dt.strftime("%A, %d-%b-%Y")
    today_iso = now_dt.strftime("%Y-%m-%d")
    current_year = now_dt.year
    return f"""You are a Facilities management assistant for Good Earth Infra (GEI).
You help the Facilities team manage tasks across buildings: GEBB1, GEBB2, GETT, and Common areas.

CURRENT SYSTEM DATE & TIME:
- Today is: {today_str} (ISO: {today_iso})
- Current Year: {current_year}
- All relative dates ("today", "tomorrow", "next Monday", "in 3 days", "this Friday") MUST be calculated strictly relative to TODAY ({today_iso}).
- Never use past years like 2024 or 2025.

{FACILITIES_SYSTEM_PROMPT_BODY}"""


FACILITIES_SYSTEM_PROMPT = _get_facilities_system_prompt()

REPLY_SYSTEM_PROMPT = """You are GEI Facilities Bot — a professional, friendly WhatsApp assistant
for the Good Earth Infra Facilities team.

Generate conversational replies for WhatsApp messages. Keep the tone:
- Professional but warm
- Concise (WhatsApp messages should be scannable)
- Use emojis sparingly but effectively (📋, ✅, 🔧, 🏗️, etc.)
- Use WhatsApp bold (*text*) for emphasis
- Never use markdown headers or code blocks in replies
- Always be direct — state what happened, what's next

You are NOT generating JSON. You are generating a natural language WhatsApp message."""


# ── Core LLM Callers ─────────────────────────────────────────────────────────

def _call_gemini(system_prompt: str, user_message: str, json_output: bool = True) -> str:
    """Execute API request against Gemini."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{FACILITIES_GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{
            "parts": [{"text": f"{system_prompt}\n\nUser message: {user_message}"}]
        }]
    }
    if json_output:
        payload["generationConfig"] = {"response_mime_type": "application/json"}

    r = requests.post(url, json=payload, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data['candidates'][0]['content']['parts'][0]['text']


def _call_groq(system_prompt: str, user_message: str, json_output: bool = True) -> str:
    """Execute API request against Groq with automatic model fallback."""
    if not GROQCLOUD_API_KEY:
        raise RuntimeError("GROQCLOUD_API_KEY is not configured")

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQCLOUD_API_KEY}",
        "Content-Type": "application/json"
    }

    models_to_try = [FACILITIES_GROQ_MODEL] + [m for m in FACILITIES_GROQ_FALLBACK_MODELS if m != FACILITIES_GROQ_MODEL]
    last_err = None

    for model_name in models_to_try:
        try:
            payload = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message}
                ],
                "temperature": 0
            }
            if json_output:
                payload["response_format"] = {"type": "json_object"}

            r = requests.post(url, headers=headers, json=payload, timeout=8)
            r.raise_for_status()
            data = r.json()
            return data['choices'][0]['message']['content']
        except Exception as e:
            logger.warning(f"Groq model '{model_name}' failed: {e}")
            last_err = e

    raise last_err or RuntimeError("All Groq models failed")


def _call_llm(system_prompt: str, user_message: str, json_output: bool = True) -> str:
    """Unified LLM caller prioritizing Groq for ultra-fast response, falling back to Gemini."""
    if GROQCLOUD_API_KEY:
        providers = ["groq", "gemini"]
    else:
        providers = ["gemini", "groq"]

    last_error = None
    for provider in providers:
        try:
            if provider == "groq":
                return _call_groq(system_prompt, user_message, json_output=json_output)
            else:
                return _call_gemini(system_prompt, user_message, json_output=json_output)
        except Exception as e:
            logger.warning(f"Facilities LLM provider '{provider}' failed: {e}")
            last_error = e

    raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")


# ── Intent Extraction ────────────────────────────────────────────────────────

def extract_intent(message: str, context: dict = None) -> dict:
    """
    Extract intent and entities from a user message using Groq / Gemini.
    """
    try:
        user_msg = message
        if context:
            user_msg = f"Context: {json.dumps(context)}\n\nUser message: {message}"

        system_prompt = _get_facilities_system_prompt()
        content = _call_llm(system_prompt, user_msg, json_output=True)
        result = _parse_json_response(content)

        if result and "intents" in result:
            # Post-process and sanitize dates against current date
            from datetime import datetime
            from core.utils import parse_human_date
            today = datetime.now()
            for item in result.get("intents", []):
                entities = item.get("entities", {})
                td = entities.get("target_date")
                if td:
                    td_str = str(td).strip()
                    try:
                        # If year is in the past or date is invalid, re-parse from user message
                        if len(td_str) >= 4 and int(td_str[:4]) < today.year:
                            reparsed = parse_human_date(message)
                            if reparsed:
                                entities["target_date"] = reparsed
                    except Exception:
                        pass
                else:
                    # If LLM missed date but user text has a date keyword
                    reparsed = parse_human_date(message)
                    if reparsed:
                        entities["target_date"] = reparsed

            return result

        return {"intents": [{"intent": "unknown", "confidence": 0.0, "entities": {}}]}

    except Exception as e:
        logger.error(f"Facilities intent extraction failed: {e}", exc_info=True)
        return _fallback_intent_extraction(message)


def generate_reply(template: str, context: dict = None) -> str:
    """
    Generate a conversational WhatsApp reply using Groq / Gemini.
    """
    try:
        user_msg = template
        if context:
            user_msg = f"Data: {json.dumps(context, default=str)}\n\nGenerate a reply for: {template}"

        content = _call_llm(REPLY_SYSTEM_PROMPT, user_msg, json_output=False)
        return content.strip()

    except Exception as e:
        logger.error(f"Facilities reply generation failed: {e}")
        return template  # Fall back to the raw template


def extract_voice_operations(transcript: str) -> dict:
    """
    Extract multiple task operations from a voice note transcription.
    """
    try:
        voice_prompt = f"""The following is a transcribed voice note from a Facilities team member.
Extract ALL task operations mentioned. Each operation should be a separate entry.

Transcription: "{transcript}"

For each operation, extract:
- intent: create_task, update_task, reassign_task, or attach_file
- entities: building, type, issue_action, owner, target_date, status, ref_no, latest_update
- confidence: how confident you are in THIS specific extraction (0.0-1.0)

Also assess overall transcription_confidence:
- 1.0: Crystal clear, all words understood
- 0.7-0.9: Mostly clear, minor ambiguity
- 0.5-0.7: Some unclear parts, may need clarification
- Below 0.5: Significant uncertainty, should ask for clarification

Return JSON:
{{
  "operations": [...],
  "transcription_confidence": 0.85,
  "raw_transcript": "..."
}}"""

        content = _call_llm(FACILITIES_SYSTEM_PROMPT, voice_prompt, json_output=True)
        result = _parse_json_response(content)

        if result:
            # Map intents to operations if LLM returned standard intents array
            if "operations" not in result and "intents" in result:
                result["operations"] = result["intents"]
                result["transcription_confidence"] = max(
                    [op.get("confidence", 0.85) for op in result["operations"]], default=0.85
                )

            if "operations" in result and result["operations"]:
                result["raw_transcript"] = transcript
                return result

        return {
            "operations": [],
            "transcription_confidence": 0.5,
            "raw_transcript": transcript,
        }

    except Exception as e:
        logger.error(f"Voice operations extraction failed: {e}")
        return {
            "operations": [],
            "transcription_confidence": 0.0,
            "raw_transcript": transcript,
        }


def check_duplicate(building: str, issue_text: str,
                     existing_tasks: list) -> Optional[dict]:
    """
    Check if a new task might be a duplicate of an existing open task using Groq / Gemini.
    """
    if not existing_tasks:
        return None

    try:
        existing_summary = "\n".join([
            f"- Ref {t.get('ref_no')}: {t.get('issue_action')} (Status: {t.get('status')})"
            for t in existing_tasks[:20]  # Limit to 20 tasks
        ])

        prompt = f"""Compare this new task against existing open tasks in {building}.

NEW TASK: "{issue_text}"

EXISTING OPEN TASKS:
{existing_summary}

If any existing task is clearly about the SAME issue (even if worded differently),
return JSON:
{{"is_duplicate": true, "matching_ref_no": "GEBB1-XXX", "similarity_reason": "brief explanation"}}

If no clear match:
{{"is_duplicate": false}}

Be conservative — only flag true duplicates, not vaguely similar tasks."""

        content = _call_llm(FACILITIES_SYSTEM_PROMPT, prompt, json_output=True)
        result = _parse_json_response(content)

        if result and result.get("is_duplicate"):
            ref_no = result.get("matching_ref_no")
            # Find the full task data
            for task in existing_tasks:
                if task.get("ref_no") == ref_no:
                    task["similarity_reason"] = result.get("similarity_reason", "")
                    return task

        return None

    except Exception as e:
        logger.error(f"Duplicate check failed: {e}")
        return None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_json_response(text: str) -> dict | None:
    """Extract JSON from an LLM response that might contain surrounding text."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    import re
    json_match = re.search(r'\{[\s\S]*\}', text)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass

    return None


def _fallback_intent_extraction(message: str) -> dict:
    """
    Rule-based fallback when LLM is unavailable.
    """
    msg = message.strip().lower()

    # Greeting
    if msg in ("hi", "hello", "hey", "menu", "start"):
        return {"intents": [{"intent": "greeting", "confidence": 0.95, "entities": {}}]}

    # Overdue tasks
    if any(kw in msg for kw in ["overdue", "over due", "past due", "delayed"]):
        entities = {}
        # Extract building if mentioned
        from facilities.auth import fuzzy_match_building
        for word in msg.split():
            bldg = fuzzy_match_building(word)
            if bldg:
                entities["building"] = bldg
                break
        entities["overdue"] = True
        return {"intents": [{"intent": "show_overdue_tasks", "confidence": 0.85, "entities": entities}]}

    # Employee name-based task queries
    try:
        from facilities.owner_resolver import match_employee_name
        # Check for patterns like "show Vikash tasks", "Vikramjeet's tasks"
        for name_pattern in [
            r"(?:show|view|list|get)\s+(\w+)(?:'s)?\s+tasks?",
            r"tasks?\s+(?:for|of|assigned\s+to)\s+(\w+)",
            r"(\w+)(?:'s)?\s+(?:overdue|pending|completed|tasks?)",
        ]:
            m = re.search(name_pattern, msg, re.IGNORECASE)
            if m:
                potential_name = m.group(1)
                matched = match_employee_name(potential_name)
                if matched:
                    entities = {"employee_name": matched}
                    # Check if overdue
                    if "overdue" in msg:
                        entities["overdue"] = True
                        return {"intents": [{"intent": "filter_tasks", "confidence": 0.8, "entities": entities}]}
                    # Check for building
                    bldg_match = fuzzy_match_building(msg)
                    if bldg_match:
                        entities["building"] = bldg_match
                    return {"intents": [{"intent": "filter_tasks", "confidence": 0.8, "entities": entities}]}
    except Exception:
        pass

    # Status-based queries: "show pending tasks", "show completed tasks", etc.
    status_keywords = {
        "pending": "Open",
        "in progress": "WIP",
        "completed": "Closed",
        "blocked": "Escalated",
        "on hold": "On Hold",
    }
    for keyword, status_val in status_keywords.items():
        if keyword in msg and any(w in msg for w in ["task", "tasks", "show", "view", "list"]):
            entities = {"status": status_val}
            from facilities.auth import fuzzy_match_building
            for word in msg.split():
                bldg = fuzzy_match_building(word)
                if bldg:
                    entities["building"] = bldg
                    break
            return {"intents": [{"intent": "filter_tasks", "confidence": 0.8, "entities": entities}]}

    # Future tasks
    if any(kw in msg for kw in ["future task", "future tasks", "upcoming task", "upcoming tasks"]):
        entities = {"future": True}
        return {"intents": [{"intent": "filter_tasks", "confidence": 0.8, "entities": entities}]}

    # View intents
    if any(kw in msg for kw in ["my task", "my tasks"]):
        return {"intents": [{"intent": "view_my_tasks", "confidence": 0.8, "entities": {}}]}

    if any(kw in msg for kw in ["team task", "team tasks"]):
        return {"intents": [{"intent": "view_team_tasks", "confidence": 0.8, "entities": {}}]}

    if any(kw in msg for kw in ["summary", "rollup", "overview"]):
        return {"intents": [{"intent": "view_summary", "confidence": 0.8, "entities": {}}]}

    if any(kw in msg for kw in ["sync", "sync status", "integration"]):
        return {"intents": [{"intent": "view_sync_status", "confidence": 0.8, "entities": {}}]}

    if any(kw in msg for kw in ["history", "audit", "log"]):
        return {"intents": [{"intent": "view_history", "confidence": 0.7, "entities": {}}]}

    if any(kw in msg for kw in ["digest", "daily"]):
        return {"intents": [{"intent": "daily_digest", "confidence": 0.7, "entities": {}}]}

    # Action intents
    if any(kw in msg for kw in ["create", "new task", "add task", "raise"]):
        return {"intents": [{"intent": "create_task", "confidence": 0.75, "entities": {}}]}

    if any(kw in msg for kw in ["update", "change status", "mark as"]):
        return {"intents": [{"intent": "update_task", "confidence": 0.75, "entities": {}}]}

    if any(kw in msg for kw in ["reassign", "assign to", "transfer"]):
        return {"intents": [{"intent": "reassign_task", "confidence": 0.75, "entities": {}}]}

    if any(kw in msg for kw in ["help", "how to", "what can"]):
        return {"intents": [{"intent": "help", "confidence": 0.9, "entities": {}}]}

    # Check for ref_no pattern
    import re
    ref_match = re.search(r'(GEBB[12]|GETT|Common)-(\d{3})', msg, re.IGNORECASE)
    if ref_match:
        ref_no = ref_match.group(0).upper()
        return {"intents": [{"intent": "view_task_detail", "confidence": 0.8, "entities": {"ref_no": ref_no}}]}

    return {"intents": [{"intent": "unknown", "confidence": 0.0, "entities": {}}]}
