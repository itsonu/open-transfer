"""The transfer lifecycle: which state may follow which, and why a transfer ended.

Senders and receivers keep their own live state (``mesh.py`` uses the older
wire names, see :func:`open_transfer.history.lifecycle_state`), but every change
goes through :func:`can_move`, and every unhappy ending carries a reason code
from :data:`REASONS` so the UI never has to guess from free text.

::

    offered ──► accepted ──► transferring ──► completed
       │           │              ├─────────► partial   (some files arrived)
       │           │              ├─────────► failed
       │           └──────────────┴─────────► cancelled / failed
       └──► declined / expired / cancelled / failed

Deadlines (``mesh.py``): offered 120 s (+15 s on the sender, which hears it
second), accepted 90 s without a first byte, transferring 60 s without data.
"""

from __future__ import annotations

from open_transfer.history import (
    ACCEPTED,
    CANCELLED,
    COMPLETED,
    DECLINED,
    EXPIRED,
    FAILED,
    FINAL_STATES,
    OFFERED,
    PARTIAL,
    TRANSFERRING,
)

_NEXT: dict[str, frozenset[str]] = {
    OFFERED: frozenset({ACCEPTED, DECLINED, EXPIRED, CANCELLED, FAILED}),
    ACCEPTED: frozenset({TRANSFERRING, COMPLETED, PARTIAL, CANCELLED, FAILED}),
    TRANSFERRING: frozenset({COMPLETED, PARTIAL, CANCELLED, FAILED}),
}


def can_move(old: str, new: str) -> bool:
    """True if a transfer in lifecycle state ``old`` may change to ``new``.

    Final states never change. ``accepted`` may finish without passing through
    ``transferring`` (empty files, or a sender that learns the outcome late).
    """
    if old in FINAL_STATES:
        return False
    return old == new or new in _NEXT.get(old, frozenset())


#: reason code → (what happened, what the user can do)
REASONS: dict[str, tuple[str, str]] = {
    "receiver_declined": ("The receiver declined.", "Ask them to accept, then send again."),
    "insufficient_storage": (
        "There isn’t enough free space on the receiving device.",
        "Free up space there, or send fewer files.",
    ),
    "file_too_large": (
        "A file is larger than the receiving device accepts.",
        "Send a smaller file, or raise the limit on that device.",
    ),
    "permission_denied": (
        "The receiving device only accepts files from paired devices.",
        "Pair the two devices first.",
    ),
    "sender_cancelled": ("The sender stopped the transfer.", "Ask them to send again."),
    "receiver_cancelled": (
        "The receiver stopped the transfer.",
        "Send again if they still want it.",
    ),
    "sender_disconnected": (
        "The sending device stopped sending.",
        "Keep Open Transfer open on both devices and send again.",
    ),
    "receiver_disconnected": (
        "Lost the connection to the receiving device.",
        "Check that it’s nearby and on the same Wi-Fi, then retry.",
    ),
    "receiver_unreachable": (
        "Couldn’t reach the receiving device.",
        "Check that Open Transfer is open on it and on the same Wi-Fi, then retry.",
    ),
    "network_timeout": (
        "The network stopped responding.",
        "Check the Wi-Fi on both devices, then retry.",
    ),
    "transfer_stalled": (
        "No data arrived for a minute.",
        "Keep both devices awake and on the same Wi-Fi, then retry.",
    ),
    "expired": ("Nobody answered within 2 minutes.", "Send again when they’re ready to accept."),
    "app_closed": (
        "Open Transfer was closed during the transfer.",
        "Send again; transfers start over from the beginning.",
    ),
    "app_restart": (
        "Open Transfer stopped during the transfer.",
        "Send again; transfers start over from the beginning.",
    ),
    "destination_unavailable": (
        "The receiving device couldn’t save the file.",
        "Check its Open Transfer folder still exists, then retry.",
    ),
    "invalid_request": (
        "The other device sent something unexpected.",
        "Update Open Transfer on both devices.",
    ),
    "unknown_failure": ("Something went wrong.", "Try again."),
    # Pairing (docs/trust-model.md)
    "pairing_code_invalid": (
        "That code isn’t the one the other device is showing.",
        "Check the code on its screen; codes change every 10 minutes.",
    ),
    "pairing_expired": (
        "That pairing attempt has ended.",
        "Open Add device on the other device and try again.",
    ),
    "pairing_not_open": (
        "None of the nearby devices is showing a pairing code.",
        "Open Add device on the other device first.",
    ),
    "discovery_unavailable": (
        "No nearby devices could be found automatically; this network may block it.",
        "Scan the QR code on the other device, or enter the address it shows.",
    ),
    "device_unreachable": (
        "That device can’t be reached.",
        "Check it’s on the same Wi-Fi with Open Transfer open.",
    ),
    "trust_rejected": (
        "The other device didn’t allow the pairing.",
        "Ask its owner to press Allow.",
    ),
    "identity_mismatch": (
        "The device at that address couldn’t prove it’s the one you chose.",
        "Pair again from the device itself; if this repeats, something on the network may be interfering.",
    ),
    "address_verification_failed": (
        "A device claimed to be a paired device but couldn’t prove it.",
        "Nothing to do; it was ignored.",
    ),
    "already_paired": ("These devices are already paired.", "Nothing to do."),
    "peer_unpaired": ("The other device removed this pairing.", "Pair again if you want to."),
    "rate_limited": ("Too many wrong codes.", "Wait a minute, then check the code and try again."),
}


def describe(code: str, detail: str = "") -> dict[str, str]:
    """``{reason_code, reason, action}`` for an API response; ``detail`` overrides the text."""
    if not code:
        return {"reason_code": "", "reason": detail, "action": ""}
    message, action = REASONS.get(code, REASONS["unknown_failure"])
    return {"reason_code": code, "reason": detail or message, "action": action}
