"""Trusted Sync Network: copy on one device, paste on the others.

See ``docs/sync.md`` for the design. The pieces:

- :mod:`.seal` — encrypting payloads for one recipient (stdlib only).
- :mod:`.clock` — the hybrid logical clock that orders copies across devices.
- :mod:`.protocol` — envelope fields, validation and limits.
- :mod:`.store` — policies, sequence numbers, tombstones and queued control messages.
- :mod:`.engine` — the rules: fan-out, dedup, loops, staleness, expiry, trust.
"""
