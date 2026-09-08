"""Closed, non-sensitive user preferences, separate from provider credentials."""
THINKING_LEVELS = ("low", "medium", "high", "xhigh")
THINKING_SCOPES = ("chat", "research")
DEFAULT_THINKING = "low"


def validate_thinking(value: object) -> str:
    if type(value) is not str or value not in THINKING_LEVELS:
        raise ValueError("invalid thinking level")
    return value


def validate_scope(value: object) -> str:
    if type(value) is not str or value not in THINKING_SCOPES:
        raise ValueError("invalid thinking scope")
    return value
