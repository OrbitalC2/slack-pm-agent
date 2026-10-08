"""Task priority normalization helpers."""

VALID_PRIORITIES = {"low", "medium", "high", "urgent"}


def clean_priority(priority: str | None) -> str:
    """Return a DB-safe task priority."""
    if priority in VALID_PRIORITIES:
        return priority
    return "medium"
