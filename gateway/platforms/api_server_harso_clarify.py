"""DEC-415: a spoken reply during a call answers the turn's pending clarify.

A call's handoff reaches the cell as an internal submit. While the agent waits on clarify, the
running turn cannot read a steer and a queued turn waits behind it, so the reply must answer the
waiter. Spoken text is not typed text: "Business class flights.", "two", "the second one" and
"Skip." all name a choice. Any other reply is passed on in the caller's own words ("Other"), so a
rejection or a redirect ("not flights, research trains") reaches the agent intact.
"""
import re
from typing import Callable, Optional

_NUMBERS = {word: index for index, word in enumerate(
    ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"), 1)}
_NUMBERS.update({word: index for index, word in enumerate(
    ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"), 1)})
_PICK = re.compile(r"(?:(?:the|option|number|choice)\s+)*(\w+)(?:\s+(?:one|option|choice))?")


def _words(text: str) -> str:
    from tools.clarify_tool import strip_recommended
    return " ".join(re.sub(r"[\W_]+", " ", strip_recommended(str(text)).casefold()).split())


def spoken_answer(text: str, choices: list) -> str:
    """The value to resolve the clarify with: a choice label, SKIPPED, or the caller's words."""
    from tools.clarify_gateway import SKIP_WORD, SKIPPED
    said = _words(text)
    if said == SKIP_WORD:
        return SKIPPED
    labels = [_words(choice) for choice in choices]
    if said in labels:
        return str(choices[labels.index(said)]).strip()
    pick = _PICK.fullmatch(said)
    token = pick.group(1) if pick else ""
    index = int(token) if token.isdigit() else _NUMBERS.get(token)
    if index is not None and 1 <= index <= len(choices):
        return str(choices[index - 1]).strip()
    return text.strip()


def pending_clarify_answer(adapter, session_key: str, message: str) -> Optional[Callable[[], bool]]:
    """A resolver that answers the session's oldest pending clarify from ``message``, or None.
    The caller records the answer durably before calling it, so a retry never answers a later prompt."""
    from tools.clarify_gateway import get_pending_for_session, resolve_gateway_clarify
    entry = get_pending_for_session(session_key, include_choice_prompts=True)
    if entry is None:
        return None
    value = spoken_answer(message, list(entry.choices or []))

    def resolve() -> bool:
        if not resolve_gateway_clarify(entry.clarify_id, value):
            return False
        for key, state in list(adapter._native_submit_clarifies.items()):
            if key[1] == entry.clarify_id and state == "pending":
                adapter._native_submit_clarifies[key] = "resolved"  # a late tap on the card is terminal
        return True
    return resolve
