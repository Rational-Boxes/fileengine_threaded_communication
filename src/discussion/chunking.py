"""Chunk comment text for embedding (SPECIFICATION §6).

Comments are short, so most produce a single chunk; long ones are split on
paragraph/sentence boundaries under a character budget. Input is the plaintext
``body_text`` projection (§4a), so there is no Markdown to preserve here.
"""
from __future__ import annotations

from typing import List

_MAX_CHARS = 1200


def chunk_text(text: str, max_chars: int = _MAX_CHARS) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks: List[str] = []
    buf = ""
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) > max_chars:                 # a single huge paragraph — hard-split
            for i in range(0, len(para), max_chars):
                chunks.append(para[i:i + max_chars])
            continue
        if len(buf) + len(para) + 2 > max_chars and buf:
            chunks.append(buf.strip())
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf.strip():
        chunks.append(buf.strip())
    return chunks
