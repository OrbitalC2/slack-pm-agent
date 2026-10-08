"""
Permission checking utilities.

Administrative access comes only from GLOBAL_ADMIN_SLACK_IDS. The stored
project-manager ID is used for message classification, not authorization.
"""

import re
from typing import Any, Literal

from pm_agent.app.agents.chains import classify_dev_ask_chain
from pm_agent.app.agents.llm_client import get_llm_client
from pm_agent.app.config import get_settings
from pm_agent.app.logging_config import get_logger

logger = get_logger("permissions")

UserRole = Literal["admin", "dev"]

ADMIN_COMMANDS = {
    "setup-project",
    "gather-info",
    "huddle-notes",
    "update",
    "status",
    "blockers",
    "ask",
    "clarify",
    "raise-clarification",
    "help",
}
DEV_COMMANDS = {
    "status",
    "blockers",
    "ask",
    "clarify",
    "raise-clarification",
    "huddle-notes",
    "help",
}

COMMAND_PERMISSION_DENIED = (
    "You do not have permission to run this command. Ask an admin to run it."
)
APPROVAL_PERMISSION_DENIED = "Only admins can approve or reject agent suggestions."
DEV_ASK_RESTRICTED_MESSAGE = (
    "This question needs admin access. You can ask about implementation scope, "
    "blockers, assigned tasks, confirmed requirements, or source references."
)

RESTRICTED_DEV_ASK_PHRASES = (
    "budget",
    "price",
    "pricing",
    "payment terms",
    "contract",
    "contract value",
    "revenue",
    "profit",
    "margin",
    "client paying",
    "invoice to client",
    "salary",
    "promised deadline",
    "client commitment",
    "commitment",
    "what should we tell the client",
    "draft client reply",
    "draft a client reply",
    "send to client",
    "scope negotiation",
    "other client",
    "other project",
    "private notes",
    "ceo notes",
    "admin notes",
    "all client documents",
)


def _normalize_slack_user_id(user_id: str | None) -> str:
    """Normalize Slack user IDs before comparing env-derived values."""
    return (user_id or "").strip()


def _admin_slack_ids() -> set[str]:
    """Return normalized global admin Slack IDs from settings."""
    settings = get_settings()
    return {
        _normalize_slack_user_id(user_id)
        for user_id in settings.admin_slack_ids
        if _normalize_slack_user_id(user_id)
    }


def is_project_approver(user_id: str, project) -> bool:
    """
    Compatibility wrapper for older call sites.

    Project-level approver roles are intentionally not used for command access.
    """
    return is_global_admin(user_id)


def is_global_admin(user_id: str) -> bool:
    """Check if a user is a global admin."""
    normalized_user_id = _normalize_slack_user_id(user_id)
    return bool(normalized_user_id and normalized_user_id in _admin_slack_ids())


def get_user_role(slack_user_id: str) -> UserRole:
    """Return the global access role for a Slack user."""
    return "admin" if is_global_admin(slack_user_id) else "dev"


def can_run_command(slack_user_id: str, command_name: str) -> bool:
    """Return whether the Slack user can run a /agent subcommand."""
    normalized_command = (command_name or "").strip().lower()
    if get_user_role(slack_user_id) == "admin":
        return normalized_command in ADMIN_COMMANDS
    return normalized_command in DEV_COMMANDS


def can_approve_suggestion(slack_user_id: str) -> bool:
    """Return whether the Slack user can approve/reject/edit suggestions."""
    return is_global_admin(slack_user_id)


def get_global_admin_slack_ids() -> list[str]:
    """Return configured global admin Slack IDs in stable order."""
    seen = set()
    admin_ids = []
    for user_id in get_settings().admin_slack_ids:
        normalized = _normalize_slack_user_id(user_id)
        if normalized and normalized not in seen:
            seen.add(normalized)
            admin_ids.append(normalized)
    return admin_ids


async def is_dev_ask_allowed(
    slack_user_id: str,
    question: str,
    project_context: Any | None = None,
) -> bool:
    """Return whether a Slack user may run a dev-scoped ask query."""
    if is_global_admin(slack_user_id):
        return True

    if _is_restricted_dev_ask_by_keyword(question):
        return False

    try:
        llm_client = get_llm_client()
        classification = await classify_dev_ask_chain(question, llm_client)
    except Exception as exc:
        logger.warning("Dev ask classification failed: %s", exc, exc_info=True)
        return False

    return classification.classification == "allowed_dev_query"


def _is_restricted_dev_ask_by_keyword(question: str) -> bool:
    """Fast path for obvious admin-only dev questions."""
    normalized = re.sub(r"\s+", " ", (question or "").strip().lower())
    if not normalized:
        return False

    if any(phrase in normalized for phrase in RESTRICTED_DEV_ASK_PHRASES):
        return True

    has_client = "client" in normalized
    has_deadline = "deadline" in normalized or "due date" in normalized
    if has_client and has_deadline and re.search(r"\b(promis\w*|commit\w*)\b", normalized):
        return True
    if has_client and re.search(r"\b(delay|delaying|change|changing)\b", normalized):
        return True
    if has_client and re.search(r"\b(tell|reply|respond|send|draft)\b", normalized):
        return True
    if re.search(r"\b(privately|private)\b", normalized):
        return True

    return False


def get_project_channel_kind(project, channel_id: str) -> str | None:
    """Return client/internal for a configured project channel."""
    if channel_id == project.client_channel_id:
        return "client"
    if project.internal_channel_id and channel_id == project.internal_channel_id:
        return "internal"
    return None


def is_internal_project_channel(project, channel_id: str) -> bool:
    """Check whether a channel is the project's internal channel."""
    return get_project_channel_kind(project, channel_id) == "internal"
