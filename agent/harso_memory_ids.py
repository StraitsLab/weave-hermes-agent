"""UUIDv7 operation IDs for the supported Python 3.11–3.14 range."""
import secrets
import time
import uuid


def new_operation_id() -> str:
    if hasattr(uuid, "uuid7"):
        return str(uuid.uuid7())
    milliseconds = time.time_ns() // 1_000_000
    return str(uuid.UUID(int=(milliseconds << 80) | (7 << 76) |
                         (secrets.randbits(12) << 64) | (2 << 62) | secrets.randbits(62)))
