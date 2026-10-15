"""Unexpected silence presentation for gateway turns."""
from agent.i18n import t

def _unexpected_silence_reply() -> str:
    """Reply when the model returned only a silence marker for a message that needed an answer."""
    return t("gateway.errors.unexpected_silence")


def _bg_prompt_preview(prompt: str, limit: int = 60) -> str:
    """Short single-line quote of a /bg prompt for its failure notice (the task id means nothing to the user)."""
    text = " ".join(str(prompt or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
