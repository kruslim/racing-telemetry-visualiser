"""Radio feed grouping: driver questions, their answers, and calls that aired after them.

A call whose ``in_reply_to`` is a driver message's ``message_id`` is its answer
(task C1: the backend routes every driver message as a question) and nests under
it wherever it lands in the feed, marked ``answer``. Other agent calls within
``THREAD_WINDOW_S`` of session time after a driver message (at most four) nest
under it as "after your message", the rule of the frozen web
``frontend/js/pitwall/radio-panel.js``, which knows no answers.
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
    """Messages in arrival order -> entries ``{message, children}``, oldest first.

    An answer child carries ``"answer": True``; ``in_reply_to`` an unknown id
    (the question scrolled out) is treated as an ordinary call.
    """
    entries: list[dict] = []
    by_id: dict[str, dict] = {}
    thread: dict | None = None
    for message in messages:
        if not message:
            continue
        if message.get("agent") == "driver":
            t = message.get("session_time")
            thread = {"message": message, "children": [], "at": t if is_num(t) else None}
            entries.append(thread)
            if message.get("message_id"):
                by_id[message["message_id"]] = thread
            continue
        asked = by_id.get(message.get("in_reply_to") or "")
        if asked is not None:
            asked["children"].append({**message, "answer": True})
        elif thread is not None and _in_window(thread, message):
            thread["children"].append(message)
        else:
            thread = None
            entries.append({"message": message, "children": [], "at": None})
    return entries[-FEED_LIMIT:]


def question_status(message: dict, children: list[dict]) -> str | None:
    """What the driver should see under their question while no answer is in.

    ``None`` once answered, or for a driver message with no routing (an old one).
    """
    if any(c.get("answer") for c in children):
        return None
    route = (message.get("data") or {}).get("question")
    if not isinstance(route, dict) or not route.get("owner"):
        return None
    owner = str(route["owner"]).replace("_", " ")
    if route.get("intent"):
        return f"asked the {owner}"
    return f"asked the {owner} · waiting for the answer"


def latest_strategist(messages: list[dict]) -> dict | None:
    """The newest strategist call that aired, for the recommendation card."""
    for message in reversed(messages):
        if (
            message
            and message.get("agent") == "strategist"
            and not message.get("expired")
            and not message.get("superseded")
        ):
            return message
    return None

