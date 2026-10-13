"""Narrow cell-local hold channel. Non-cell installs retain their native lifecycle."""
import asyncio
import json
import logging
import os
import socket
from pathlib import Path

DIRECTORY = Path("/run/hermes-awake")
SOCKET = DIRECTORY / "hold.sock"


def required():
    return DIRECTORY.exists() or os.environ.get("CELL_AWAKE_REQUIRED") == "1"


def _request(op, lease):
    if not required():
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(SOCKET))
        connection.sendall(json.dumps({"op": op, "lease": lease}).encode() + b"\n")
        if connection.recv(64) != b'{"ok":true}\n':
            raise RuntimeError("native cell hold unavailable")


async def finish_owned(operation):
    """Drain an already-started operation despite repeated caller cancellation.

    Cancelling an asyncio waiter cannot stop its to_thread worker. Callers must
    keep ownership until that worker settles, then decide cleanup vs handoff.
    """
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    return operation.result()


async def acquire(lease):
    await asyncio.to_thread(_request, "acquire", lease)


def release(lease):
    async def finish():
        try:
            await asyncio.to_thread(_request, "release", lease)
        except Exception:
            logging.getLogger(__name__).warning("native cell hold release unavailable; bounded lease expires")
    asyncio.get_running_loop().create_task(finish())
