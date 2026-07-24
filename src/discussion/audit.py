# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

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
