from __future__ import annotations
import logging
from db import supabase
from whatsapp.ux import clean_phone_number
from elara.config import DEVELOPER_PHONE, KANAV_PHONE, KANAV_PHONES

logger = logging.getLogger(__name__)


def resolve_elara_user(whatsapp_number: str) -> dict | None:
    """
    Resolve a WhatsApp number to an Elara Home user.
    Queries ONLY the `elara_users` table in Supabase.
    """
    clean_num = clean_phone_number(whatsapp_number)
    if not clean_num:
        return None

    try:
        # Check by clean phone
        res = supabase.table("elara_users").select("*").eq("phone", clean_num).execute()
        if res and isinstance(res.data, list) and len(res.data) > 0 and isinstance(res.data[0], dict):
            user = dict(res.data[0])
            user["is_elara_user"] = True
            return user

        # Also check without country code prefix (e.g. 10 digits) if applicable
        if len(clean_num) > 10 and clean_num.startswith("91"):
            short_num = clean_num[2:]
            res = supabase.table("elara_users").select("*").eq("phone", short_num).execute()
            if res and isinstance(res.data, list) and len(res.data) > 0 and isinstance(res.data[0], dict):
                user = dict(res.data[0])
                user["is_elara_user"] = True
                return user

        # Special check for Developer number or Kanav number
        if clean_num == DEVELOPER_PHONE:
            res = supabase.table("elara_users").select("*").eq("role", "Developer").execute()
            if res.data and isinstance(res.data, list) and isinstance(res.data[0], dict):
                user = dict(res.data[0])
                user["is_elara_user"] = True
                return user
            return {"id": "elara-dev-01", "name": "Developer", "role": "Developer", "phone": clean_num, "department": "Project Administration", "is_elara_user": True}

        elif clean_num in KANAV_PHONES or clean_num == KANAV_PHONE:
            res = supabase.table("elara_users").select("*").eq("name", "Kanav").execute()
            if res.data and isinstance(res.data, list) and isinstance(res.data[0], dict):
                user = dict(res.data[0])
                user["is_elara_user"] = True
                return user
            return {"id": "elara-kanav-01", "name": "Kanav", "role": "Director", "phone": clean_num, "department": "Project Administration", "is_elara_user": True}

        return None
    except Exception as e:
        logger.error(f"resolve_elara_user failed for {whatsapp_number}: {e}")
        return None


def is_elara_user(user_or_phone: str | dict | None) -> bool:
    """Check if user dict or WhatsApp number belongs to Elara Home team."""
    if not user_or_phone:
        return False
    if isinstance(user_or_phone, dict):
        if user_or_phone.get("team") == "Elara Home" or user_or_phone.get("is_elara_user"):
            return True
        phone = user_or_phone.get("phone") or user_or_phone.get("whatsapp_number")
        if not phone:
            return False
        user = resolve_elara_user(str(phone))
        return user is not None and user.get("is_elara_user", False)
    # pyrefly: ignore [unnecessary-type-conversion]
    user = resolve_elara_user(str(user_or_phone))
    return user is not None and user.get("is_elara_user", False)



def is_elara_admin(user: dict | None) -> bool:
    """Director (Kanav) and Developer have full cross-department access."""
    if not user:
        return False
    role = (user.get("role") or "").strip().lower()
    name = (user.get("name") or "").strip().lower()
    phone = clean_phone_number(user.get("phone") or user.get("whatsapp_number"))

    if role in ("director", "developer"):
        return True
    if name in ("kanav", "developer", "abhijeet"):
        return True
    if phone in (DEVELOPER_PHONE, KANAV_PHONE):
        return True
    return False


def can_access_department(user: dict | None, department: str) -> bool:
    """Check if user is permitted to access the given department."""
    if not user:
        return False
    if is_elara_admin(user):
        return True
    user_dept = (user.get("department") or "").strip()
    if not user_dept or user_dept.lower() == department.strip().lower():
        return True
    return False


CANONICAL_ELARA_MEMBERS = [
    {"name": "Rachit", "phone": "919867272041", "role": "Team Member", "department": "Construction & Design", "team": "Elara Home"},
    {"name": "Bhagwan Dass", "phone": "919816641892", "role": "Team Member", "department": "Construction & Design", "team": "Elara Home"},
    {"name": "Gaurav", "phone": "918894577707", "role": "Team Member", "department": "Construction & Design", "team": "Elara Home"},
    {"name": "Developer", "phone": "917717754421", "role": "Developer", "department": "Project Administration", "team": "Elara Home"},
    {"name": "Kanav", "phone": "919811867829", "role": "Director", "department": "Project Administration", "team": "Elara Home"},
    {"name": "Rizwan", "phone": "919717856493", "role": "Team Member", "department": "Construction & Design", "team": "Elara Home"},
    {"name": "Raja Nadeem", "phone": "917006116371", "role": "Team Member", "department": "Construction & Design", "team": "Elara Home"},
    {"name": "Sayangdeep Das", "phone": "916290721639", "role": "Team Member", "department": "Construction & Design", "team": "Elara Home"},
    {"name": "Puja", "phone": "", "role": "Team Member", "department": "Construction & Design", "team": "Elara Home"},
]


def get_elara_team_members(department: str | None = None) -> list:
    """Get all Elara team members, optionally filtered by department."""
    try:
        query = supabase.table("elara_users").select("*")
        if department:
            query = query.eq("department", department)
        res = query.execute()
        if res and res.data and len(res.data) > 0:
            return res.data
    except Exception as e:
        logger.error(f"get_elara_team_members failed: {e}")

    # Fallback to canonical list when database is unreachable or mock client is used
    if department:
        return [m for m in CANONICAL_ELARA_MEMBERS if m.get("department", "").lower() == department.strip().lower()]
    return list(CANONICAL_ELARA_MEMBERS)


def resolve_elara_assignee(name_query: str) -> dict | None:
    """
    Resolve an assignee name against the Elara Home database (`elara_users` and `users` table in Supabase).
    Returns the user dict if found, or None if the user does not exist in Supabase.
    """
    if not name_query or not isinstance(name_query, str):
        return None
    clean_query = name_query.strip()
    if not clean_query:
        return None

    clean_lower = clean_query.lower()
    if clean_lower in ("me", "myself", "self", "assign to me", "keep", "skip", "current"):
        return None

    try:
        # 1. Exact match (case-insensitive) on elara_users
        res = supabase.table("elara_users").select("*").ilike("name", clean_query).execute()
        if res.data and len(res.data) > 0:
            return res.data[0]

        # 2. Substring/prefix match on elara_users
        res_sub = supabase.table("elara_users").select("*").ilike("name", f"%{clean_query}%").execute()
        if res_sub.data and len(res_sub.data) > 0:
            return res_sub.data[0]

        # 3. Check against all elara_users for first name or partial match
        all_members = get_elara_team_members()
        for m in all_members:
            m_name = (m.get("name") or "").lower()
            if clean_lower == m_name or clean_lower in m_name or m_name in clean_lower:
                return m
            if clean_lower in m_name.split():
                return m

        # 4. Check general users table as fallback
        res_u = supabase.table("users").select("*").ilike("name", clean_query).execute()
        if res_u.data and len(res_u.data) > 0:
            return res_u.data[0]

        res_u_sub = supabase.table("users").select("*").ilike("name", f"%{clean_query}%").execute()
        if res_u_sub.data and len(res_u_sub.data) > 0:
            return res_u_sub.data[0]

        # 5. Check against canonical members fallback
        for m in CANONICAL_ELARA_MEMBERS:
            m_name = m["name"].lower()
            if clean_lower == m_name or clean_lower in m_name or m_name in clean_lower:
                return m
            if clean_lower in m_name.split():
                return m

        return None
    except Exception as e:
        logger.error(f"resolve_elara_assignee failed for '{name_query}': {e}")
        for m in CANONICAL_ELARA_MEMBERS:
            m_name = m["name"].lower()
            if clean_lower == m_name or clean_lower in m_name or m_name in clean_lower:
                return m
            if clean_lower in m_name.split():
                return m
        return None

