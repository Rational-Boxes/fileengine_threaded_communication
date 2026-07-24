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

"""MCP toolset operations (§9 / M5) — hermetic, reusing the REST test fakes.

The tools act as the caller's identity and enforce the same permission model as
the REST surface; they share the mention-safety primitive (targets.validate_targets).
"""
import pytest

from discussion.ldap_auth import Identity
from discussion.mcp_tools import Components, ToolError, Toolset

from .test_reviews import FakeReviewStore
from .test_threads import FakeDirectory, FakeEvents, FakeIndexer, FakeNotes, FakePerms, FakeStore


def _toolset(reads=True, writes=None, deny_users=frozenset(), directory=None):
    store = FakeStore()
    comps = Components(
        permissions=FakePerms(reads=reads, writes=writes, deny_users=deny_users),
        store=store,
        directory=directory or FakeDirectory(),
        notifications=FakeNotes(),
        events=FakeEvents(),
        reviews=FakeReviewStore(),
        indexer=FakeIndexer(),
    )
    return Toolset(comps), comps


def _id(user, roles=None):
    return Identity(user=user, roles=roles or ["users"], tenant="default", authenticated=True)


def test_list_threads_permission():
    ts, _ = _toolset(reads=None)
    with pytest.raises(ToolError):
        ts.list_threads(_id("bob"), "f1")


def test_open_and_get_thread():
    ts, c = _toolset(reads=True)
    thread = ts.open_thread(_id("bob"), "f1", body="**hi**", title="Q")
    assert thread["file_uid"] == "f1" and thread["opened_by"] == "bob"
    assert any(x["comment_id"] == thread["comments"][0]["id"] for x in c.indexer.indexed)
    assert "thread.opened" in c.events.types()
    got = ts.get_thread(_id("carol"), thread["id"])
    assert len(got["comments"]) == 1


def test_post_comment_with_valid_mention():
    ts, c = _toolset(reads=True, directory=FakeDirectory({"carol@x": "carol"}))
    tid = ts.open_thread(_id("bob"), "f1", body="x")["id"]
    comment = ts.post_comment(_id("bob"), tid, body="ping", mentions=["carol@x"])
    assert comment["author"] == "bob"
    assert "mention" in c.notifications.kinds_for("carol")
    assert "mention.created" in c.events.types()


def test_post_comment_error_marks_bad_mention():
    ts, _ = _toolset(reads=True, deny_users={"carol"}, directory=FakeDirectory({"carol@x": "carol"}))
    tid = ts.open_thread(_id("bob"), "f1", body="x")["id"]
    with pytest.raises(ToolError) as e:
        ts.post_comment(_id("bob"), tid, body="ping", mentions=["carol@x"])
    assert "carol@x" in str(e.value)


def test_resolve_requires_opener_or_write():
    ts, c = _toolset(reads=True, writes=None)
    tid = ts.open_thread(_id("bob"), "f1", body="x")["id"]
    with pytest.raises(ToolError):
        ts.resolve_thread(_id("carol"), tid)          # not opener, no write
    resolved = ts.resolve_thread(_id("bob"), tid, resolved_version="v2")  # opener
    assert resolved["status"] == "resolved" and resolved["resolved_version"] == "v2"


def test_raise_review_valid_and_error_marked():
    ts, c = _toolset(reads=True, directory=FakeDirectory({"carol@x": "carol"}))
    reviews = ts.raise_review(_id("bob"), "f1", ["carol@x"])
    assert reviews[0]["reviewer"] == "carol"
    assert "review_requested" in c.notifications.kinds_for("carol")

    ts2, _ = _toolset(reads=True, deny_users={"carol"}, directory=FakeDirectory({"carol@x": "carol"}))
    with pytest.raises(ToolError):
        ts2.raise_review(_id("bob"), "f1", ["carol@x"])
