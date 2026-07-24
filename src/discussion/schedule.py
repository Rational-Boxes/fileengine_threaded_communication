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

"""Digest scheduling logic (SPECIFICATION §11c) — pure, so it's unit-testable.

The cron fires the sender **hourly**; the per-user ``cadence`` decides who is due
this hour. ``period_key`` is the idempotency bucket (one delivery per user per
period). Daily/weekly boundaries are evaluated in the user's timezone; hourly in
UTC. All functions take an explicit aware ``now_utc`` so tests are deterministic.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def local_now(now_utc: dt.datetime, tz_name: str) -> dt.datetime:
    return now_utc.astimezone(_tz(tz_name))


def is_due(cadence: str, *, send_hour_local: int, send_dow: int, tz_name: str,
           now_utc: dt.datetime) -> bool:
    """Whether a subscription is due on this hourly run."""
    if cadence == "hourly":
        return True
    if cadence not in ("daily", "weekly"):
        return False
    ln = local_now(now_utc, tz_name)
    if ln.hour != int(send_hour_local):
        return False
    if cadence == "daily":
        return True
    # weekly: send_dow uses 0=Sunday (schema); Python weekday() is 0=Monday.
    dow_sun0 = (ln.weekday() + 1) % 7
    return dow_sun0 == int(send_dow)


def period_key(cadence: str, *, tz_name: str, now_utc: dt.datetime) -> str:
    """The canonical bucket for a user's cadence (the UNIQUE idempotency key)."""
    if cadence == "hourly":
        return now_utc.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H")
    ln = local_now(now_utc, tz_name)
    if cadence == "weekly":
        iso = ln.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    return ln.strftime("%Y-%m-%d")  # daily


def since_for(cadence: str, now_utc: dt.datetime) -> dt.datetime:
    """Lookback window for gathering items for one digest."""
    delta = {
        "hourly": dt.timedelta(hours=1),
        "daily": dt.timedelta(days=1),
        "weekly": dt.timedelta(days=7),
    }.get(cadence, dt.timedelta(days=1))
    return now_utc - delta
