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

"""Thread provenance descriptor (§12 / M5)."""
from discussion.provenance import thread_permalink, thread_provenance


def test_permalink():
    assert thread_permalink("f1", "t1", "https://x.test/") == "https://x.test/preview/f1?thread=t1"
    assert thread_permalink("f1", "t1", "") == "/preview/f1?thread=t1"


def test_thread_provenance_shape():
    thread = {
        "id": "t1", "file_uid": "f1", "version": "", "status": "resolved",
        "opened_by": "bob", "resolved_by": "carol", "resolved_version": "v3",
        "created_at": "ts",
        "comments": [
            {"author": "bob", "body": "x"},
            {"author": "carol", "body": "y"},
            {"author": "bob", "body": "z"},   # duplicate author collapses
        ],
    }
    p = thread_provenance(thread, spa_base_url="https://x.test")
    assert p["source_type"] == "discussion_thread"
    assert p["thread_id"] == "t1" and p["file_uid"] == "f1"
    assert p["participants"] == ["bob", "carol"]     # opener first, deduped
    assert p["resolved_version"] == "v3"             # the backward-provenance link
    assert p["permalink"] == "https://x.test/preview/f1?thread=t1"
    assert p["version"] is None                       # empty version → null
