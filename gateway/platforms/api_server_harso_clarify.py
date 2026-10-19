"""DEC-415: a spoken reply during a call answers the turn's pending clarify.

A call's handoff reaches the cell as an internal submit. While the agent waits on clarify, the
running turn cannot read a steer and a queued turn waits behind it, so the reply must answer the
waiter. Spoken text is not typed text: "Business class flights.", "the second one", "two" and
"Skip." all name a choice. Anything that names no choice is the caller's own answer ("Other"),
never a reason to keep the turn blocked.
"""
import json
import re
from typing import Optional

_NUMBERS = {word: index for index, word in enumerate(
    ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"), 1)}
_NUMBERS.update({word: index for index, word in enumerate(
    ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"), 1)})
_PICK = re.compile(r"(?:(?:the|option|number|choice)\s+)*(\w+)(?:\s+(?:one|option|choice))?")


def _words(text: str) -> str:
    from tools.clarify_tool import strip_recommended
    return " ".join(re.sub(r"[\W_]+", " ", strip_recommended(str(text)).casefold()).split())


def spoken_answer(text: str, choices: list, multi_select: bool) -> str:
    """The value to resolve the clarify with: a choice label, SKIPPED, a JSON array of labels for
    multi-select, or the caller's words verbatim when they name no single choice."""
    from tools.clarify_gateway import SKIP_WORD, SKIPPED
    said = _words(text)
    if said.split()[:1] == [SKIP_WORD]:
        return SKIPPED
    if not choices:
        return text.strip()
    labels = [_words(choice) for choice in choices]
    pick = _PICK.fullmatch(said)
    index = None
    if pick:
        token = pick.group(1)
        index = int(token) if token.isdigit() else _NUMBERS.get(token)
    if index is not None and 1 <= index <= len(choices):
        named = [choices[index - 1]]
    elif said in labels:
        named = [choices[labels.index(said)]]
    else:
        named = [choice for choice, label in zip(choices, labels)
                 if label and re.search(rf"\b{re.escape(label)}\b", said)]
    if multi_select and named:
        return json.dumps([str(choice).strip() for choice in named], ensure_ascii=False)
    return str(named[0]).strip() if len(named) == 1 else text.strip()


def answer_pending_clarify(adapter, session_key: str, message: str) -> Optional[str]:
    """Resolve the session's oldest pending clarify from ``message``; its clarify id, or None."""
    from tools.clarify_gateway import get_pending_for_session, resolve_gateway_clarify
    entry = get_pending_for_session(session_key, include_choice_prompts=True)
    if entry is None:
        return None
    value = spoken_answer(message, list(entry.choices or []), bool(entry.multi_select))
    if not resolve_gateway_clarify(entry.clarify_id, value):
        return None
    for key, state in list(adapter._native_submit_clarifies.items()):
        if key[1] == entry.clarify_id and state == "pending":
            adapter._native_submit_clarifies[key] = "resolved"  # a late tap on the card is terminal
    return entry.clarify_id
