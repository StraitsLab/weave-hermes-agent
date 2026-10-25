"""Native submit attachments reuse the gateway's bounded, SSRF-safe media downloader."""
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit


def valid_attachments(attachments):
    if not isinstance(attachments, list) or len(attachments) > 10:
        return False
    for attachment in attachments:
        if not isinstance(attachment, dict) or set(attachment) != {"url", "media_type", "name"}:
            return False
        url, media, name = (attachment[key] for key in ("url", "media_type", "name"))
        if (not all(isinstance(value, str) for value in (url, media, name))
                or not 1 <= len(url) <= 16_384 or not 1 <= len(name) <= 1024
                or not re.fullmatch(r"[\w.+-]+/[\w.+-]+", media)
                or re.search(r"[\x00-\x20\x7f]", url)
                or re.search(r"[/\\\x00-\x1f\x7f]", name) or name in {".", ".."}):
            return False
        try:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                return False
            parsed.port  # Reject malformed ports before admission.
        except ValueError:
            return False
    return True


async def cache_attachments(attachments):
    from gateway.platforms import base
    paths, types = [], []
    cache_functions = {"image": base.cache_image_from_bytes, "audio": base.cache_audio_from_bytes,
                       "video": base.cache_video_from_bytes, "document": base.cache_document_from_bytes}
    for attachment in attachments:
        media, name = attachment["media_type"], attachment["name"]
        kind = media.split("/", 1)[0]
        if kind not in {"image", "audio", "video"}:
            kind = "document"
        # Documents retain the filename; the other cache helpers accept an extension.
        suffix = name if kind == "document" else PurePosixPath(name).suffix
        try:
            path = await base._cache_media_from_url(
                attachment["url"], suffix, 0, media_type=kind, accept=media,
                cache_fn=cache_functions[kind], log_label="Native attachment")
        except Exception:  # health: allow BLE001 -- re-raised as a typed refusal; the cause is dropped because a signed URL is a credential
            # Signed URLs are credentials; the submit error logger must not print transport exceptions.
            raise RuntimeError("Native attachment download failed") from None
        paths.append(path)
        types.append(media)
    return paths, types
