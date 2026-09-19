from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any


class EventBroker:
    def __init__(self, max_events: int = 256):
        self._condition = threading.Condition()
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._seq = 0

    def emit(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._condition:
            self._seq += 1
            event = {"seq": self._seq, "type": event_type, "payload": payload}
            self._events.append(event)
            self._condition.notify_all()
            return event

    def wait_after(self, after: int, timeout: float = 25.0) -> tuple[int, list[dict[str, Any]]]:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            # The daemon sequence restarts from zero after a service restart.
            # A browser may still present a larger persisted cursor; reset it.
            if after > self._seq:
                after = 0
            while True:
                events = [event for event in self._events if int(event["seq"]) > after]
                if events or time.monotonic() >= deadline:
                    return self._seq, events
                self._condition.wait(timeout=max(0.0, deadline - time.monotonic()))
