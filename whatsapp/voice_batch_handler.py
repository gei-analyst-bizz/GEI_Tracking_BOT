"""
Voice Batch Update Parser
==========================
Extracts multiple task updates from a single voice note transcript.

Uses the Groq LLM (same provider as intent parser) to parse Hinglish
transcripts like:
  "Top Terrace mein waterproofing 60% ho gaya, tile wala 70%, 
   aur 9th floor mein tile installation complete ho gaya"

Into structured updates:
  [
    {"project": "Top Terrace", "task": "waterproofing", "progress": 60},
    {"project": "Top Terrace", "task": "tile", "progress": 70},
    {"project": "9th Floor", "task": "tile installation", "progress": 100, "completed": true}
  ]
"""

import os
import re
import json
import logging
import requests
from typing import Optional, List

logger = logging.getLogger(__name__)

GROQCLOUD_API_KEY = os.getenv("GROQCLOUD_API_KEY", "")

BATCH_EXTRACT_PROMPT = """You extract multiple task updates from a voice transcript.
The user speaks in Hinglish (Hindi-English mix) about construction project tasks.

RULES:
* Extract EVERY task update mentioned, even if brief
* "hogaya", "khatam", "complete", "done", "finished" → completed=true, progress=100
* If progress is mentioned (e.g. "60%", "70 percent"), extract the number
* If project is mentioned (e.g. "Top Terrace", "9th floor", "Bay 1"), extract it
* If project is NOT mentioned for a task, set project_name to null
* "blocker", "issue", "problem", "nahi ho raha" → extract as blocker text
* Return a JSON array. Each element has:
  - project_name: string or null
  - task_name: string (the task being referenced)
  - progress: integer (0-100) or null if not mentioned
  - completed: boolean (true only if explicitly marked done)
  - blocker: string or null

EXAMPLES:

Transcript: "Top Terrace mein waterproofing 60% ho gaya aur tile wala 70% ho gaya aur 9th floor mein tile installation complete ho gaya"
Output: [
  {"project_name": "Top Terrace", "task_name": "waterproofing", "progress": 60, "completed": false, "blocker": null},
  {"project_name": "Top Terrace", "task_name": "tile", "progress": 70, "completed": false, "blocker": null},
  {"project_name": "9th floor", "task_name": "tile installation", "progress": 100, "completed": true, "blocker": null}
]

Transcript: "bay 1 waterproofing 80% done, slope correction mein blocker hai material nahi aaya, aur solar panel complete"
Output: [
  {"project_name": "bay 1", "task_name": "waterproofing", "progress": 80, "completed": false, "blocker": null},
  {"project_name": null, "task_name": "slope correction", "progress": null, "completed": false, "blocker": "material nahi aaya"},
  {"project_name": null, "task_name": "solar panel", "progress": 100, "completed": true, "blocker": null}
]

Return ONLY the JSON array, nothing else.
"""


def is_batch_transcript(transcript: str) -> bool:
    """
    Detect if a transcript likely contains multiple task updates.
    Uses simple heuristics before making an expensive LLM call.
    
    Key rule: avoid false positives. A single "waterproofing 60% ho gaya" 
    should NOT be detected as batch. Only trigger when there are clearly
    distinct task references separated by connectors.
    """
    t = transcript.lower()

    # Count progress mentions (e.g. "60%", "70 percent", "50 %")
    progress_mentions = len(re.findall(r'\d+\s*%|\d+\s*percent', t))
    
    # Count STANDALONE completion keywords (only count distinct ones)
    # "complete", "khatam", "finished" — NOT "ho gaya" which is too common
    strong_complete = len(re.findall(
        r'\b(complete|khatam|finished|done)\b', t
    ))

    # Count connectors that separate updates
    connectors = len(re.findall(r'\b(aur|and|also|plus|phir|then)\b', t))

    # Multiple distinct progress percentages → definitely batch
    if progress_mentions >= 2:
        return True

    # One progress + one distinct completion + connector → batch
    if progress_mentions >= 1 and strong_complete >= 1 and connectors >= 1:
        return True

    # Multiple strong completions + connector → batch
    if strong_complete >= 2 and connectors >= 1:
        return True

    return False


def extract_batch_updates(transcript: str) -> Optional[List[dict]]:
    """
    Use Groq LLM to extract multiple task updates from a voice transcript.
    
    Returns:
        List of update dicts, or None if extraction fails.
        Each dict: {project_name, task_name, progress, completed, blocker}
    """
    try:
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
                        {"role": "system", "content": BATCH_EXTRACT_PROMPT},
                        {"role": "user", "content": f'Transcript: "{transcript}"'}
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0
                }

                response = requests.post(url, headers=headers, json=payload, timeout=10)
                response.raise_for_status()
                data = response.json()
                content = data['choices'][0]['message']['content']
                result = json.loads(content)
                if isinstance(result, dict):
                    result = result.get("updates") or result.get("data") or list(result.values())[0]
                if not isinstance(result, list) or len(result) < 1:
                    return None
                return result
            except Exception as e:
                logger.warning(f"Groq model '{model_name}' failed in batch handler: {e}")
                last_err = e
        if last_err:
            raise last_err
        return None

        # Validate each update has required fields
        validated = []
        for item in result:
            if not isinstance(item, dict):
                continue
            if not item.get("task_name"):
                continue
            validated.append({
                "project_name": item.get("project_name"),
                "task_name": str(item["task_name"]).strip(),
                "progress": int(item["progress"]) if item.get("progress") is not None else None,
                "completed": bool(item.get("completed", False)),
                "blocker": str(item["blocker"]).strip() if item.get("blocker") else None,
            })

        return validated if validated else None

    except Exception as e:
        logger.error(f"Batch extract failed: {e}")
        return None
