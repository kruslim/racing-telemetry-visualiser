"""Radio feed grouping: driver messages and the calls that aired after them.

The same rule as ``frontend/js/pitwall/radio-panel.js``: agent calls within
``THREAD_WINDOW_S`` of session time after a driver message (at most four) nest
under it as "after your message". Never "reply": the backend does not route driver
messages to the agents as questions.
"""

from __future__ import annotations

from .format import is_num

THREAD_WINDOW_S = 45.0
THREAD_LIMIT = 4
FEED_LIMIT = 200


def _in_window(thread: dict, message: dict) -> bool:
    if len(thread["children"]) >= THREAD_LIMIT:
        return False
    at, t = thread["at"], message.get("session_time")
    if at is None or not is_num(t):
        return not thread["children"]
    return t - at <= THREAD_WINDOW_S


def thread_feed(messages: list[dict]) -> list[dict]:
    """Messages in arrival order -> entries ``{message, children}``, oldest first."""
    entries: list[dict] = []
    thread: dict | None = None
    for message in messages:
        if not message:
            continue
        if message.get("agent") == "driver":
            t = message.get("session_time")
            thread = {"message": message, "children": [], "at": t if is_num(t) else None}
            entries.append(thread)
        elif thread is not None and _in_window(thread, message):
            thread["children"].append(message)
        else:
            thread = None
            entries.append({"message": message, "children": [], "at": None})
    return entries[-FEED_LIMIT:]


def latest_strategist(messages: list[dict]) -> dict | None:
    """The newest strategist call, for the recommendation card."""
    for message in reversed(messages):
        if message and message.get("agent") == "strategist":
            return message
    return None
