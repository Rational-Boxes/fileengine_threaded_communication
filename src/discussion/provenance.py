"""Thread provenance descriptor (SPECIFICATION §12 / M5).

Phase 1's provenance schema gains a source type ``discussion_thread`` so an AI
report that drew on commentary can cite the thread — permalink + anchor — and
"which reports drew on the discussion of document X" is answerable. This builds
that descriptor from a thread record. The ``resolved_version`` is the backward-
provenance link: the file version that addressed the discussion.
"""
from __future__ import annotations


def thread_permalink(file_uid: str, thread_id: str, spa_base_url: str = "") -> str:
    base = (spa_base_url or "").rstrip("/")
    return f"{base}/preview/{file_uid}?thread={thread_id}"


def thread_provenance(thread: dict, *, spa_base_url: str = "") -> dict:
    file_uid = thread.get("file_uid", "")
    thread_id = thread.get("id", "")
    opener = thread.get("opened_by")

    participants: list[str] = []
    if opener:
        participants.append(opener)
    for c in thread.get("comments") or []:
        author = c.get("author")
        if author and author not in participants:
            participants.append(author)

    return {
        "source_type": "discussion_thread",
        "thread_id": thread_id,
        "file_uid": file_uid,
        "version": thread.get("version") or None,
        "status": thread.get("status"),
        "opened_by": opener,
        "participants": participants,
        "resolved_by": thread.get("resolved_by"),
        # The version that addressed the discussion — the backward-provenance link.
        "resolved_version": thread.get("resolved_version"),
        "permalink": thread_permalink(file_uid, thread_id, spa_base_url),
        "created_at": thread.get("created_at"),
    }
