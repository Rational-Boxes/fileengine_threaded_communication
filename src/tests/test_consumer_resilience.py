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

"""The consumer must outlive a transient Redis error.

redis-py 8 defaults socket_timeout to 5s and this consumer blocks for 5000ms, so
on an idle stream the socket deadline and the server's "no events" reply landed
together. Only handle() was guarded, so the timeout escaped run_forever and ended
the process — exit code 0, which under `restart_policy: always` reads as a clean
stop. In production that was a container restarting every few minutes, 183 times,
with nothing that looked like an error.
"""
import redis

from discussion.consumer import SOCKET_TIMEOUT_S, RedisEventSource


class Boom:
    """Raises on the first N reads, then behaves."""

    def __init__(self, exc, times=1):
        self.exc, self.left, self.reads, self.resets = exc, times, 0, 0

    def ensure_group(self):
        pass

    def read(self, *a, **k):
        self.reads += 1
        if self.left > 0:
            self.left -= 1
            raise self.exc
        raise KeyboardInterrupt      # stop the loop deterministically

    def ack(self, ids):
        pass

    def reset(self):
        self.resets += 1


def _consumer():
    from discussion.consumer import EventConsumer
    return EventConsumer.__new__(EventConsumer)   # no store/config needed for these


def _run(source, monkeypatch):
    import discussion.consumer as mod
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)   # no real backoff
    mod.EventConsumer.run_forever(_consumer(), source)


def test_a_socket_timeout_does_not_end_the_consumer(monkeypatch):
    src = Boom(redis.exceptions.TimeoutError("Timeout reading from socket"))
    _run(src, monkeypatch)
    assert src.reads == 2          # survived the timeout and read again
    assert src.resets == 1         # and rebuilt the connection first


def test_a_dropped_connection_does_not_end_the_consumer(monkeypatch):
    src = Boom(redis.exceptions.ConnectionError("Connection closed by server"))
    _run(src, monkeypatch)
    assert src.reads == 2


def test_repeated_failures_keep_being_retried(monkeypatch):
    src = Boom(redis.exceptions.ConnectionError("still down"), times=5)
    _run(src, monkeypatch)
    assert src.reads == 6
    assert src.resets == 5


def test_a_failing_cycle_backs_off_before_retrying(monkeypatch):
    """Without a pause, an unreachable Redis would be retried in a hot loop."""
    import discussion.consumer as mod
    slept = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: slept.append(s))
    mod.EventConsumer.run_forever(
        _consumer(), Boom(redis.exceptions.ConnectionError("down"), times=3))
    assert slept == [mod.CYCLE_BACKOFF_S] * 3


def test_the_socket_timeout_leaves_room_over_the_block():
    """The whole point: the socket must not give up while the server is still
    within its blocking window."""
    assert SOCKET_TIMEOUT_S > 5, "read() blocks 5000ms; the socket must outlast it"


def test_read_treats_a_timeout_as_no_events(monkeypatch):
    """Belt and braces for a caller that blocks longer than the socket allows."""
    src = RedisEventSource.__new__(RedisEventSource)
    src.stream, src.group, src.consumer = "s", "g", "c"

    class C:
        def xreadgroup(self, *a, **k):
            raise redis.exceptions.TimeoutError("Timeout reading from socket")

    monkeypatch.setattr(src, "_client", lambda: C())
    assert src.read() == []
