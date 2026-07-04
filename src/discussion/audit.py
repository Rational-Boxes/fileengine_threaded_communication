"""Lightweight structured audit log.

Governance-relevant actions that are hidden from peers but must remain accountable —
administrator **redaction** (§5b) and (M4) admin **invisible viewing** (§10h) — are
recorded here. Writes go to ``DISC_AUDIT_LOG_FILE`` if set, else the audit logger
(stderr by default). Never raises into a request.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging

log = logging.getLogger("discussion.audit")

_configured_file = ""


def configure(path: str) -> None:
    global _configured_file
    _configured_file = path or ""
    if _configured_file:
        handler = logging.FileHandler(_configured_file)
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)


def record(action: str, **fields) -> None:
    entry = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(), "action": action, **fields}
    try:
        log.info(json.dumps(entry, default=str))
    except Exception:
        pass
