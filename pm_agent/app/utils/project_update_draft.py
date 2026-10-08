"""Formatting helpers for client-ready project update drafts."""


def build_project_update_data(
    project_name: str,
    task_summary: dict,
    pending_clarifications: list,
    approved_decisions: list,
    approved_risks: list,
    approved_assumptions: list,
    github_summary: str = "",
) -> str:
    """Build compact text context for the project update LLM chain."""
    tasks = task_summary.get("tasks", [])
    done_tasks = [t for t in tasks if getattr(t, "status", None) == "done"]
    doing_tasks = [t for t in tasks if getattr(t, "status", None) == "doing"]
    blocked_tasks = [t for t in tasks if getattr(t, "status", None) == "blocked"]
    review_tasks = [t for t in tasks if getattr(t, "status", None) == "review"]

    sections = [
        f"Project: {project_name}",
        (
            "Task counts: "
            f"{task_summary.get('done', 0)} done, "
            f"{task_summary.get('doing', 0)} in progress, "
            f"{task_summary.get('blocked', 0)} blocked, "
            f"{task_summary.get('review', 0)} in review, "
            f"{task_summary.get('todo', 0)} todo."
        ),
        _object_list_section("Completed tasks", done_tasks),
        _object_list_section("In-progress tasks", doing_tasks),
        _object_list_section("In-review tasks", review_tasks),
        _object_list_section("Blocked tasks", blocked_tasks),
        _object_list_section("Pending client clarifications", pending_clarifications, "question"),
        _object_list_section("Approved decisions", approved_decisions),
        _object_list_section("Acknowledged scope risks", approved_risks),
        _object_list_section("Unconfirmed assumptions", approved_assumptions),
    ]

    if github_summary:
        sections.append(f"GitHub activity:\n{github_summary}")
    else:
        sections.append("GitHub activity: No repository configured or no activity available.")

    return "\n\n".join(section for section in sections if section)


def client_safe_decisions(decisions: list) -> list:
    """
    Return decisions safe for client-facing update drafts.

    Decisions generated from internal gathers carry payload.channel_kind='internal'
    and may be implementation notes rather than client commitments.
    """
    safe = []
    for decision in decisions:
        payload = getattr(decision, "payload", None) or {}
        if payload.get("channel_kind") == "internal":
            continue
        safe.append(decision)
    return safe


def format_project_update_draft(project_name: str, output) -> str:
    """Format a ProjectUpdateOutput-like object as a Slack DM draft."""
    lines = [
        f"📝 *Draft Update: {project_name}*",
        "_Review and edit before sharing with the client. This was not sent automatically._",
    ]

    summary = getattr(output, "summary", "")
    if summary:
        lines.extend(["", "*Summary:*", summary])

    _append_bullet_section(lines, "Completed", getattr(output, "completed_items", []))
    _append_bullet_section(lines, "In Progress", getattr(output, "in_progress", []))
    _append_bullet_section(lines, "Blockers / Clarifications", getattr(output, "blockers", []))
    _append_bullet_section(lines, "Next Steps", getattr(output, "next_steps", []))

    return "\n".join(lines)


def format_fallback_project_update(
    project_name: str,
    task_summary: dict,
    pending_clarifications: list,
    github_summary: str = "",
) -> str:
    """Build a deterministic update draft when the LLM chain is unavailable."""
    lines = [
        f"📝 *Draft Update: {project_name}*",
        "_Review and edit before sharing with the client. This was not sent automatically._",
        "",
        "*Progress:*",
        f"• {task_summary.get('done', 0)} tasks completed",
        f"• {task_summary.get('doing', 0)} tasks in progress",
        f"• {task_summary.get('blocked', 0)} tasks blocked",
    ]

    tasks = task_summary.get("tasks", [])
    _append_object_section(lines, "Completed", [t for t in tasks if getattr(t, "status", None) == "done"])
    _append_object_section(lines, "In Progress", [t for t in tasks if getattr(t, "status", None) == "doing"])
    _append_object_section(lines, "Blocked", [t for t in tasks if getattr(t, "status", None) == "blocked"])
    _append_object_section(lines, "Items Needing Clarification", pending_clarifications, "question")

    if github_summary:
        lines.extend(["", "*GitHub Activity:*", github_summary])

    return "\n".join(lines)


def _object_list_section(title: str, objects: list, attr: str = "title", limit: int = 10) -> str:
    if not objects:
        return f"{title}: none"
    lines = [f"{title}:"]
    for obj in objects[:limit]:
        value = getattr(obj, attr, None) or getattr(obj, "title", None) or getattr(obj, "body", "")
        body = getattr(obj, "body", "")
        if body and attr != "body":
            lines.append(f"- {value}: {body}")
        else:
            lines.append(f"- {value}")
    return "\n".join(lines)


def _append_bullet_section(lines: list[str], title: str, items: list[str]) -> None:
    if not items:
        return
    lines.extend(["", f"*{title}:*"])
    for item in items[:10]:
        lines.append(f"• {item}")


def _append_object_section(
    lines: list[str],
    title: str,
    objects: list,
    attr: str = "title",
) -> None:
    if not objects:
        return
    lines.extend(["", f"*{title}:*"])
    for obj in objects[:10]:
        lines.append(f"• {getattr(obj, attr, '')}")
