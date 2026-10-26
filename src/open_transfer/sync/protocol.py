"""The ``sync/1`` envelope: fields, limits and validation.

Every envelope is sealed for exactly one recipient (``dst``) by the device
that put it on the network (``by``). ``kind`` is ``event`` (a clipboard
copy), ``intro`` (meet another member), ``revoke`` (forget a member) or
``join`` (introduce me to whoever I don't know yet).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, cast

from open_transfer.devices import valid_id

VERSION = 1
KINDS = ("event", "intro", "revoke", "join")
TYPES = ("text", "url")
MAX_TEXT_BYTES = 1024 * 1024
#: Pending-event expiry the owner can choose (seconds).
MIN_EXPIRY, MAX_EXPIRY, DEFAULT_EXPIRY = 30, 3600, 120
#: Control messages (introductions, revocations) wait this long for an offline member.
CONTROL_EXPIRY = 7 * 24 * 3600
#: Accepted clock difference for ``created`` (the signature window is ±120 s too).
MAX_SKEW = 300
MAX_VIA = 1

_EVENT_ID = re.compile(r"[0-9a-f]{32}")
_URL = re.compile(r"(?i)(?:https?|ftp|mailto|tel|sms):[^\s<>\"]+|www\.[^\s<>\"]+\.[^\s<>\"]+")
HEADER_FIELDS = ("v", "kind", "id", "src", "by", "dst", "seq", "hlc", "type", "created", "expires")


class SyncError(Exception):
    """A request the sync engine refuses (maps to an HTTP error)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def classify(text: str) -> str:
    """``url`` when the whole clip is one link, else ``text``."""
    return "url" if _URL.fullmatch(text.strip()) else "text"


@dataclass(frozen=True)
class Header:
    v: int
    kind: str
    id: str
    src: str
    by: str
    dst: str
    seq: int
    hlc: tuple[int, int]
    type: str
    created: float
    expires: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "kind": self.kind,
            "id": self.id,
            "src": self.src,
            "by": self.by,
            "dst": self.dst,
            "seq": self.seq,
            "hlc": [self.hlc[0], self.hlc[1]],
            "type": self.type,
            "created": self.created,
            "expires": self.expires,
        }

    @property
    def order(self) -> tuple[int, int, str]:
        """Newest wins: hybrid clock first, then the source device id."""
        return self.hlc[0], self.hlc[1], self.src


def _bad(message: str) -> SyncError:
    return SyncError(400, "bad_request", message)


def parse_header(env: object) -> Header:
    if not isinstance(env, dict):
        raise _bad("Expected an envelope.")
    if env.get("v") != VERSION:
        raise SyncError(400, "version", "Unsupported sync version.")
    kind = env.get("kind")
    if kind not in KINDS:
        raise _bad("Unknown kind.")
    event_id, src, by, dst = env.get("id"), env.get("src"), env.get("by"), env.get("dst")
    if not (isinstance(event_id, str) and _EVENT_ID.fullmatch(event_id)):
        raise _bad("Bad event id.")
    if not (valid_id(src) and valid_id(by) and valid_id(dst)):
        raise _bad("Bad device id.")
    seq, hlc, created, expires = (
        env.get("seq"),
        env.get("hlc"),
        env.get("created"),
        env.get("expires"),
    )
    if not (isinstance(seq, int) and not isinstance(seq, bool) and 0 <= seq < 2**63):
        raise _bad("Bad sequence number.")
    if not (
        isinstance(hlc, list)
        and len(hlc) == 2
        and all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x < 2**63 for x in hlc)
    ):
        raise _bad("Bad clock.")
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (created, expires)):
        raise _bad("Bad times.")
    created, expires = float(cast("float", created)), float(cast("float", expires))
    limit = CONTROL_EXPIRY if kind != "event" else MAX_EXPIRY
    if not (created <= expires <= created + limit):
        raise _bad("Bad expiry.")
    kind_type = env.get("type")
    if kind == "event" and kind_type not in TYPES:
        raise SyncError(415, "type", "Unsupported content type.")
    if kind != "event" and kind_type != "":
        raise _bad("Bad type.")
    return Header(
        VERSION,
        str(kind),
        event_id,
        str(src),
        str(by),
        str(dst),
        seq,
        (hlc[0], hlc[1]),
        str(kind_type),
        float(created),
        float(expires),
    )


def parse_via(env: dict[str, Any]) -> list[str]:
    via = env.get("via", [])
    if not (isinstance(via, list) and len(via) <= MAX_VIA and all(valid_id(v) for v in via)):
        raise _bad("Bad route.")
    return [str(v) for v in via]
