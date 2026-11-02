"""
Elara Home Module Configuration
===============================
Constants, departments, statuses, and options for Elara Home.
"""

# The 6 canonical departments in Elara Home (as per Supabase & Kanban)
ELARA_DEPARTMENTS = [
    "Sales & CRM",
    "Construction & Design",
    "Approvals & Compliance",
    "Finance & Procurement",
    "Marketing & Customer Experience",
    "Project Administration",
]

# Department descriptions
DEPARTMENT_DESCRIPTIONS = {
    "Sales & CRM": "Leads, follow-ups, site visits, bookings, payment milestones, documentation.",
    "Construction & Design": "Site execution, drawings, consultant decisions, contractor actions, quality and safety issues.",
    "Approvals & Compliance": "TCP/RERA matters, government approvals, licences, statutory submissions and renewals.",
    "Finance & Procurement": "Budgets, purchase orders, vendor payments, quotations, billing and cost approvals.",
    "Marketing & Customer Experience": "Campaigns, brochures, events, website updates, buyer communication and handover preparation.",
    "Project Administration": "Hiring, manpower, meetings, reporting, travel, office/site requirements and general coordination.",
}

# Department colors
DEPARTMENT_COLORS = {
    "Sales & CRM": "#3b82f6",
    "Construction & Design": "#10b981",
    "Approvals & Compliance": "#8b5cf6",
    "Finance & Procurement": "#f59e0b",
    "Marketing & Customer Experience": "#ec4899",
    "Project Administration": "#06b6d4",
}

# Task Statuses (matches Kanban frontend TaskStatus)
VALID_STATUSES = ["pending", "in_progress", "delay", "blocker", "completed"]

STATUS_DISPLAY_NAMES = {
    "pending": "Pending",
    "in_progress": "In Progress",
    "delay": "Delayed",
    "blocker": "Blocker",
    "completed": "Completed",
}

# Priorities
VALID_PRIORITIES = ["low", "medium", "high"]

# Key phone numbers
DEVELOPER_PHONE = "917717754421"
KANAV_PHONE = "919811867829"
KANAV_PHONES = (KANAV_PHONE,)
RACHIT_PHONE = "919867272041"

def is_kanav_phone(phone: str) -> bool:
    if not phone:
        return False
    # pyrefly: ignore[unnecessary-type-conversion]
    digits = "".join(c for c in str(phone) if c.isdigit())
    if len(digits) == 10:
        digits = "91" + digits
    return digits == KANAV_PHONE

