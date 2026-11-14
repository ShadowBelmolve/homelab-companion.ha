"""Protocol 2 of the Companion: what travels inside the encrypted session.

In both directions the plain text is lines ended by a line feed; each line is
one JSON object in UTF-8, at most LINE_MAX bytes, whose `t` names the message.
A field the receiver does not know is ignored and a message it does not know
is dropped, so either side can grow first. Every time is in milliseconds.

The wire is specified in the firmware repo: apps/companion/SPEC.md. Nothing
here imports Home Assistant: a new message is one builder below.
"""

import json

# Binds the handshake to this protocol and its version.
PROLOGUE = b"homelab-companion/2"
# The most bytes of a line, the line feed not counted; the Companion drops a
# longer one.
LINE_MAX = 1024
# The Companion cuts a notification's texts to these many characters.
TITLE_MAX = 80
BODY_MAX = 200


def encode(message: dict) -> bytes:
    """The line of `message`; ValueError if it cannot be one (too long, or text that is not Unicode)."""
    line = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
    if len(line) > LINE_MAX:
        raise ValueError(f"the message takes {len(line)} bytes, the Companion reads at most {LINE_MAX}")
    return line + b"\n"


def decode(line: bytes) -> dict | None:
    """The message of a line; None for what is not one."""
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message if isinstance(message, dict) and isinstance(message.get("t"), str) else None


def notify(kind: str, urgent: bool, ms: int, title: str, body: str = "") -> dict:
    """One notification: `kind` picks the screen, `urgent` the queue, `ms` the most it stays on screen."""
    # Trimmed and cut as the Companion does it, so a long text is not a line too long.
    return {"t": "notify", "kind": kind, "urgent": urgent, "ms": ms, "title": title.strip()[:TITLE_MAX], "body": body.strip()[:BODY_MAX]}
