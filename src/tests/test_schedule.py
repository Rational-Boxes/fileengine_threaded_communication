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

"""Digest scheduling logic (§11c) — pure, deterministic."""
import datetime as dt

from discussion.schedule import is_due, period_key, since_for

UTC = dt.timezone.utc


def _t(y, m, d, h, tz=UTC):
    return dt.datetime(y, m, d, h, 0, 0, tzinfo=tz)


def test_hourly_always_due_off_never():
    now = _t(2026, 7, 4, 15)
    assert is_due("hourly", send_hour_local=0, send_dow=0, tz_name="UTC", now_utc=now)
    assert not is_due("off", send_hour_local=0, send_dow=0, tz_name="UTC", now_utc=now)


def test_daily_due_at_configured_hour():
    now = _t(2026, 7, 4, 8)
    assert is_due("daily", send_hour_local=8, send_dow=0, tz_name="UTC", now_utc=now)
    assert not is_due("daily", send_hour_local=9, send_dow=0, tz_name="UTC", now_utc=now)


def test_daily_respects_timezone():
    # 15:00 UTC == 08:00 America/Los_Angeles (PDT, UTC-7) in July.
    now = _t(2026, 7, 4, 15)
    assert is_due("daily", send_hour_local=8, send_dow=0, tz_name="America/Los_Angeles", now_utc=now)


def test_weekly_due_only_on_matching_dow():
    now = _t(2026, 7, 5, 8)
    dow_sun0 = (now.weekday() + 1) % 7            # 0 = Sunday (schema convention)
    assert is_due("weekly", send_hour_local=8, send_dow=dow_sun0, tz_name="UTC", now_utc=now)
    assert not is_due("weekly", send_hour_local=8, send_dow=(dow_sun0 + 1) % 7,
                      tz_name="UTC", now_utc=now)


def test_period_key_buckets():
    assert period_key("hourly", tz_name="UTC", now_utc=_t(2026, 7, 4, 15)) == "2026-07-04T15"
    assert period_key("daily", tz_name="UTC", now_utc=_t(2026, 7, 4, 15)) == "2026-07-04"
    assert period_key("weekly", tz_name="UTC", now_utc=_t(2026, 7, 5, 8)).startswith("2026-W")


def test_since_for_lookback():
    now = _t(2026, 7, 4, 15)
    assert since_for("hourly", now) == now - dt.timedelta(hours=1)
    assert since_for("daily", now) == now - dt.timedelta(days=1)
    assert since_for("weekly", now) == now - dt.timedelta(days=7)
