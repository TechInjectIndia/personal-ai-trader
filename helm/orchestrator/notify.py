"""
Notification dispatcher — email + optional Telegram.

PRD reference: FR-12 notifications.
"""


def alert(subject: str, body: str, *, severity: str = "info") -> None:
    """TODO Phase 6: SMTP send + optional Telegram."""
    raise NotImplementedError("notify.alert — Phase 6")


def daily_digest(stats: dict) -> None:
    """TODO Phase 6."""
    raise NotImplementedError("notify.daily_digest — Phase 6")
