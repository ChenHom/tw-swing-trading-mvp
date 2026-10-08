"""Conservative quote transport recovery (not a periodic heartbeat).

Shioaji quote transport events: 0 up, 1 down, 12 reconnecting, 13
reconnected, 16 subscription ack. SDK performs network reconnection; this
module does not log in, request account state, load a CA or submit an order.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .intraday_storage import append_raw

RECONNECT_CODES = frozenset((1, 12, 13))


def register_quote_events(api: Any, callback: Callable[[int, int, str, str], None]) -> bool:
    """Use the documented Shioaji quote callback; unsupported SDK => unverified."""
    quote = getattr(api, "quote", None)
    setter = getattr(quote, "set_event_callback", None)
    if callable(setter):
        setter(callback)
        return True
    return False


class QuoteTransportAudit:
    def __init__(self, root: Path, session_id: str) -> None:
        if not session_id or not session_id.isascii() or not all(
                ch.isalnum() or ch in "-_" for ch in session_id):
            raise ValueError("invalid session_id")
        self.root = Path(root)
        self.session_id = session_id
        self.transport_state = "UNVERIFIED"
        self.connection_verified = False
        self.gap_unresolved = False
        self.gaps = 0
        self.last_transport_event_at: str | None = None
        self.resubscribe_required = False
        self.resubscribe_failed = False

    def apply(self, response_code: int, event_code: int, at: datetime) -> bool:
        """Record transport state before any attempt to renew subscriptions.

        Return True only for SDK event 13, never for the routine sub ack 16.
        """
        if at.tzinfo is None:
            raise ValueError("transport timestamp must have timezone")
        when = at.astimezone(timezone.utc).isoformat()
        old = self.transport_state
        if event_code in (1, 12):
            self.transport_state = "RECONNECTING" if event_code == 12 else "DOWN"
            self.connection_verified = False
            self.resubscribe_required = True
            if not self.gap_unresolved:
                self.gaps += 1
            self.gap_unresolved = True
        elif event_code == 13:
            self.transport_state = "RECOVERING"
            self.connection_verified = False
            self.resubscribe_required = True
            self.gap_unresolved = True
        elif event_code == 0:
            self.transport_state = "UP"
            # Initial subscription acknowledgements are not yet evidence of a
            # continuous, gap-free feed.
            self.connection_verified = not self.gap_unresolved and not self.resubscribe_required
        self.last_transport_event_at = when
        self._write_event(response_code, event_code, old, when)
        return event_code == 13

    def recovered(self, success: bool) -> None:
        self.resubscribe_failed = not success
        self.resubscribe_required = not success
        self.transport_state = "UP" if success else "DEGRADED"
        self.connection_verified = success
        # Even successful reconnect cannot reconstruct events missed while down.
        # Do not reset this until a separate audited data-quality review.
        self.gap_unresolved = True

    def _write_event(self, response_code: int, event_code: int, old: str, when: str) -> None:
        date = when[:10]
        path = (self.root / "shioaji" / "session-events" / date /
                f"{self.session_id}.jsonl")
        append_raw(path, {
            "source": "quote_transport", "schema_version": 1,
            "session_id": self.session_id, "response_code": int(response_code),
            "event_code": int(event_code), "received_at": when,
            "from_state": old, "to_state": self.transport_state,
            "gap_unresolved": self.gap_unresolved,
        }, stop_at_disk_pct=75.0)

    def health(self) -> dict[str, Any]:
        return {
            "transport_state": self.transport_state,
            "connection_verified": self.connection_verified,
            "gap_unresolved": self.gap_unresolved,
            "gap_count": self.gaps,
            "last_transport_event_at": self.last_transport_event_at,
            "resubscribe_required": self.resubscribe_required,
            "resubscribe_failed": self.resubscribe_failed,
            # Transport events != a periodic heartbeat; no verified quote heartbeat.
            "last_heartbeat_at": None,
            "trading_session_verified": False,
        }
