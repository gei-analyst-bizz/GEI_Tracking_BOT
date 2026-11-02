import os
import logging
import json
import requests
from typing import List, Dict, Optional
from config import GEMINI_API_KEY, OPENROUTER_API_KEY, GROQCLOUD_API_KEY, LLM_PROVIDER
from core.schemas import MultiIntentResponse

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """
You are an AI assistant for a construction task management system.

ROLE:
You classify user messages into intents and extract structured data. You MUST support multiple intents from a single message if the user asks for multiple actions.

INSTRUCTIONS:
* Identify all user intents in the message.
* Extract all relevant entities for each intent.
* Return structured JSON only.
* The root of the JSON MUST be an object with an "intents" array.
* If a message follows a task update and provides context, it should be treated as a potential note.
* Support shorthand task references like "3-1" or "3.1".
* If the user assigns a task to someone by name (e.g., "for Vikash"), set 'assigned_to' to that name.
* If the user specifies the task is a personal reminder (e.g., "remind me to...", "personal task"), set 'is_personal' to true.

ALLOWED INTENTS:
* task_update: For progress updates (e.g., "60% done", "3-1 60%")
* complete_task: To mark a task as finished
* add_blocker: To report a new blocker/issue
* remove_blocker: To resolve an existing blocker
* query_tasks: To list, find, or search tasks. Use this for ALL task listing requests by date, status, or blockers.
* query_blockers: To see current blockers
* greeting: For simple greetings
* create_task: For adding new tasks or personal reminders
* create_project: For adding new projects
* create_ticket: For raising issues
* view_projects: For listing projects
* view_tickets: For showing open tickets
* trigger_reminder_user: To manually ping a user for updates (e.g., "ask asif", "send updates to asif")
* get_task_detail: To see full information about a specific task
* edit_date: To change start date or deadline of a task
* create_note: To manually add a note to a specific task
* help: For assistance

FILTER RULES:
* range: MUST be one of "overdue", "today", "tomorrow", "this_week", "custom_range", or "all" (default)
* status: MUST be one of "pending", "completed", or "all" (default)
* Use "pending" for: "ongoing", "in progress", "incomplete", "unfinished"
* Use "completed" for: "finished", "done", "closed", "marked as complete"

FEW-SHOT EXAMPLES:
Example 1:
User: "complete task 2"
Output: {"intents": [{"intent": "complete_task", "task_reference": "task 2", "confidence": 0.95}]}

Example 2 (Multiple intents):
User: "Mark generator repair as completed and add a note waiting for spare parts"
Output: {"intents": [
  {"intent": "complete_task", "task_reference": "generator repair", "confidence": 0.95},
  {"intent": "create_note", "task_reference": "generator repair", "blocker_text": "waiting for spare parts", "confidence": 0.95}
]}

Example 3 (Assignment):
User: "Create a task for Vikash to inspect the generator tomorrow"
Output: {"intents": [{"intent": "create_task", "task_reference": "inspect the generator", "assigned_to": "Vikash", "deadline": "tomorrow", "confidence": 0.98}]}

Example 4 (Personal Task):
User: "Create a personal reminder to call the vendor"
Output: {"intents": [{"intent": "create_task", "task_reference": "call the vendor", "is_personal": true, "confidence": 0.98}]}

Example 5:
User: "show my tasks"
Output: {"intents": [{"intent": "query_tasks", "confidence": 0.98, "query_filters": {"range": "all", "status": "pending"}}]}

Example 6 (Multiple Tasks Update):
User: "complete 1-2 and update 3-1 to 80% done"
Output: {"intents": [
  {"intent": "complete_task", "task_reference": "1-2", "confidence": 0.98},
  {"intent": "task_update", "task_reference": "3-1", "progress": 80, "confidence": 0.98}
]}

IMPORTANT RULES FOR create_task:
* task_reference = the NEW task name to create (not the project name)
* project_name = the project to create it under
* deadline = the deadline if mentioned
* assigned_to = user name if assigned to someone
* is_personal = true if it's a personal reminder
* NEVER put the project name in task_reference for create_task intent

OUTPUT FORMAT:
{
  "intents": [
    {
      "intent": "intent_here",
      "task_reference": "task name if any",
      "project_name": "project name if any",
      "target_user": "user name if any",
      "assigned_to": "assignee name if any",
      "is_personal": false,
      "message_type": "task_update_reminder if applicable",
      "progress": null,
      "blocker_text": "blocker text or note if any",
      "deadline": "deadline date if mentioned",
      "confidence": 1.0,
      "query_filters": {
        "range": "overdue|today|tomorrow|this_week|custom_range|all",
        "status": "pending|completed|all"
      }
    }
  ]
}
"""

def parse_with_llm(message: str, history: Optional[List[Dict]] = None, state: Optional[Dict] = None):
    # Construct context string
    context_str = ""
    if history:
        context_str += "\nRecent Conversation:\n"
        for msg in history[-5:]:
            role = "User" if msg.get('role') == 'user' else "Assistant"
            context_str += f"{role}: {msg.get('content')}\n"
    
    if state:
        context_str += f"\nCurrent State: {json.dumps(state)}\n"
    
    full_message = f"{context_str}\nCurrent Message: \"{message}\""

    provider = LLM_PROVIDER.lower()
    try:
        if provider == "gemini":
            result_json = _call_gemini(full_message, SYSTEM_PROMPT)
        elif provider == "openrouter":
            result_json = _call_openrouter(full_message, SYSTEM_PROMPT)
        elif provider == "groq":
            result_json = _call_groq(full_message, SYSTEM_PROMPT)
        else:
            logger.error(f"Unsupported LLM provider: {LLM_PROVIDER}")
            return {"intents": [{"intent": "clarify", "confidence": 0}]}

        # Pydantic Validation for MultiIntentResponse
        parsed = MultiIntentResponse.model_validate(result_json)
        return parsed.model_dump()

    except Exception as e:
        logger.error(f"LLM parsing or validation failed: {str(e)}")
        return {"intents": [{"intent": "clarify", "confidence": 0}]}

def _call_groq(message, system_prompt):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQCLOUD_API_KEY}",
        "Content-Type": "application/json"
    }
    models_to_try = [
        os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
        "openai/gpt-oss-20b",
        "qwen/qwen3.8-27b",
        "llama-3.1-8b-instant"
    ]
    last_err = None
    for model_name in models_to_try:
        try:
            payload = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message}
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0
            }
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            response.raise_for_status()
            data = response.json()
            return json.loads(data['choices'][0]['message']['content'])
        except Exception as e:
            logger.warning(f"Groq model '{model_name}' failed in core llm_parser: {e}")
            last_err = e
    raise last_err or RuntimeError("All Groq models failed in core parser")

def _call_gemini(message, system_prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"
    
    payload = {
        "contents": [{
            "parts": [{
                "text": f"{system_prompt}\n\nUser message: {message}"
            }]
        }],
        "generationConfig": {
            "response_mime_type": "application/json"
        }
    }
    
    response = requests.post(url, json=payload, timeout=10)
    response.raise_for_status()
    data = response.json()
    
    content = data['candidates'][0]['content']['parts'][0]['text']
    return json.loads(content)

def _call_openrouter(message, system_prompt):
    url = "https://openrouter.ai/api/v1/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "google/gemini-flash-1.5",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message}
        ],
        "response_format": {"type": "json_object"}
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        content = data['choices'][0]['message']['content']
        return json.loads(content)
    except Exception as e:
        logger.error(f"OpenRouter API error: {e}")
        return {"intents": [{"intent": "unknown", "confidence": 0}]}
